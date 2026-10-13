# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Router reads preserve independent ownership of the complete native aggregate."""

import copy
import uuid
from datetime import UTC, datetime

import pytest
from django.apps import apps
from django.db import IntegrityError, transaction
from ipam.models import VRF
from netbox_routing.models import BGPAddressFamily, BGPScope

from ._outbox_case import content_update, make_managed, without_commit_drain


@pytest.fixture
def router_case(db):
    return make_managed("router-read", 1767)


def router_model():
    return apps.get_model("netbox_nso_plugin", "NSOBGPRouterState")


def router_document(device, *, scopes=None, router_id="192.0.2.254"):
    return {
        "device_id": device.pk,
        "routers": [
            {
                "asn": "64512",
                "router_id": router_id,
                "scopes": scopes if scopes is not None else [scope_entry()],
            }
        ],
    }


def scope_entry(vrf="", afs=("ipv4-unicast",), peers=()):
    return {"vrf": vrf, "address_families": list(afs), "peers": list(peers), "peer_groups": []}


def peer_entry(address="192.0.2.1", af="ipv4-unicast"):
    return {
        "peer_address": address,
        "remote_as": "64513",
        "enabled": True,
        "address_families": [{"af": af, "enabled": True}],
    }


def import_router(case, **kwargs):
    from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

    device, management = case
    Model = router_model()
    peers = _reconcile_bgp_config(device, router_document(device, **kwargs))
    return Model.objects.get(management=management, asn_str="64512"), peers


def test_bgp_read_creates_router_overlay_without_peers(router_case):
    from netbox_nso_plugin.models import NSOIntentOutboxState, NSOOwnershipManifest

    state, peers = import_router(router_case)
    assert peers == []
    assert state.status == "imported"
    assert state.router_id == "192.0.2.254"
    assert state.bgp_router.asn.asn == 64512
    assert state.last_sync_at is not None
    assert len(state.device_base_hash) == 64
    assert state.accepted_at is None
    assert not NSOOwnershipManifest.objects.filter(state_model_label=state._meta.label_lower).exists()
    assert not NSOIntentOutboxState.objects.filter(device=router_case[0], scope="bgp").exists()


def test_router_identity_is_asn_across_multiple_vrf_scopes(router_case):
    VRF.objects.create(name="placeholder-blue")
    VRF.objects.create(name="placeholder-red")
    state, peers = import_router(
        router_case,
        scopes=[
            scope_entry("placeholder-red", ("ipv6-unicast",)),
            scope_entry("placeholder-blue"),
        ],
    )
    assert peers == []
    assert router_model().objects.filter(management=router_case[1]).count() == 1
    assert BGPScope.objects.filter(router=state.bgp_router).count() == 2
    with pytest.raises(IntegrityError), transaction.atomic():
        from netbox_nso_plugin.intent_state import offline_mutation

        with offline_mutation():
            router_model().objects.create(management=router_case[1], asn_str="64512")


@pytest.mark.parametrize("status", ["accepted", "deploying", "in_sync", "apply_failed"])
def test_bgp_router_reconcile_preserves_owned_bookkeeping(router_case, status):
    state, _ = import_router(router_case)
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    attempt_id = uuid.uuid4()
    with without_commit_drain():
        state = content_update(
            state,
            status=status,
            accepted_at=stamp,
            last_apply_at=stamp,
            last_apply_error="placeholder prior error",
            apply_attempt_id=attempt_id,
        )
    import_router(router_case, router_id="192.0.2.253")
    state.refresh_from_db()
    assert (state.status, state.accepted_at, state.last_apply_at, state.last_apply_error, state.apply_attempt_id) == (
        status,
        stamp,
        stamp,
        "placeholder prior error",
        attempt_id,
    )
    assert state.router_id == "192.0.2.253"
    state.bgp_router.refresh_from_db()
    assert str(state.bgp_router.router_id) == "192.0.2.254"


def test_router_read_does_not_acquire_new_device_scope_or_af(router_case):
    from django.contrib.auth import get_user_model

    from netbox_nso_plugin import delivery
    from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config, bgp_reconcile_plan
    from netbox_nso_plugin.bgp_router_content import router_content, router_content_digest
    from netbox_nso_plugin.device_differences import differences
    from netbox_nso_plugin.models import NSOBGPPeerState, NSOFamilyObservation
    from netbox_nso_plugin.read_gate import gated_family_run

    from ._routing_observation_case import routing_observation
    from ._scope_observation_case import entry
    from .test_read_gate import _rs

    device, management = router_case

    def publish(scopes, revision):
        payload = router_document(device, scopes=scopes)
        document = {
            "routers": [
                entry(
                    asn="64512",
                    router_id="192.0.2.254",
                    scope=[
                        entry(
                            vrf=scope["vrf"],
                            address_family=[entry(afi=af) for af in scope["address_families"]],
                            peer=[
                                entry(
                                    peer_address=peer["peer_address"],
                                    remote_as=peer["remote_as"],
                                    enabled=peer["enabled"],
                                    peer_address_family=[
                                        entry(afi=af["af"], enabled=af["enabled"]) for af in peer["address_families"]
                                    ],
                                )
                                for peer in scope["peers"]
                            ],
                            peer_group=[],
                        )
                        for scope in scopes
                    ],
                )
            ],
            "present": ["routers"],
            "unprojectable": [],
        }
        result = gated_family_run(
            management,
            "bgp",
            _rs(attempt_id=revision),
            lambda: _reconcile_bgp_config(device, payload),
            epoch=management.adapter_device_id,
            pre_body=lambda: bgp_reconcile_plan(device, payload),
            observation=routing_observation("bgp", document=document, revision=revision),
        )
        assert result.disposition == "ran"
        return document

    publish([scope_entry()], 1)
    state = router_model().objects.get(management=management, asn_str="64512")
    with without_commit_drain():
        state = content_update(state, status="accepted")
    before = router_content(state)
    digest = router_content_digest(before)
    rendered = delivery.render("bgp", device.pk, management.adapter_device_id).payload
    VRF.objects.create(name="placeholder-new")
    document = publish(
        [
            scope_entry(afs=("ipv4-unicast", "ipv6-unicast"), peers=[peer_entry("2001:db8::1", "ipv6-unicast")]),
            scope_entry("placeholder-new", ("ipv6-unicast",), [peer_entry("2001:db8::2", "ipv6-unicast")]),
        ],
        2,
    )
    state.refresh_from_db()
    assert router_content(state) == before
    assert router_content_digest(router_content(state)) == digest
    assert BGPScope.objects.filter(router=state.bgp_router).count() == 1
    assert list(
        BGPAddressFamily.objects.filter(scope__router=state.bgp_router).values_list("address_family", flat=True)
    ) == ["ipv4-unicast"]
    observed = list(NSOBGPPeerState.objects.filter(management=router_case[1]).order_by("peer_address_str"))
    assert len(observed) == 2
    assert all(row.bgp_peer_id is None for row in observed)
    snapshot = NSOFamilyObservation.objects.get(read_state__management=management, read_state__family="bgp")
    assert snapshot.document == document
    assert snapshot.revision == snapshot.read_state.applied_payload_revision == 2
    assert snapshot.source_epoch == snapshot.read_state.applied_source_epoch == 1
    user = get_user_model().objects.create_superuser(username="router-observer")
    device_only = {
        row.identity for row in differences(management, user=user) if row.scope == "bgp" and row.kind == "device_only"
    }
    assert ("router", 64512, "scope", "", "address_family", "ipv6-unicast") in device_only
    assert ("router", 64512, "scope", "placeholder-new") in device_only
    assert delivery.render("bgp", device.pk, management.adapter_device_id).payload == rendered


def test_missing_confirmed_router_becomes_changed(router_case):
    from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

    state, _ = import_router(router_case)
    with without_commit_drain():
        state = content_update(state, status="in_sync")
    _reconcile_bgp_config(router_case[0], {"device_id": router_case[0].pk, "routers": []})
    state.refresh_from_db()
    assert state.status == "changed"


@pytest.mark.parametrize("status", ["accepted", "deploying", "apply_failed"])
def test_missing_pending_router_preserves_intent(router_case, status):
    from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

    state, _ = import_router(router_case)
    with without_commit_drain():
        state = content_update(state, status=status)
    _reconcile_bgp_config(router_case[0], {"device_id": router_case[0].pk, "routers": []})
    state.refresh_from_db()
    assert state.status == status


def test_router_unknown_vrf_never_acquires_global_scope(router_case):
    from netbox_routing.models import BGPRouter

    from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config
    from netbox_nso_plugin.models import NSOFamilyObservation, NSOFamilyReadState, NSOOwnershipManifest
    from netbox_nso_plugin.observations import observation_defaults
    from netbox_nso_plugin.ownership_planner import OwnershipNotQualified

    from ._routing_observation_case import routing_observation
    from ._scope_observation_case import entry

    device, management = router_case
    payload = router_document(device, scopes=[scope_entry("placeholder-unknown", peers=[peer_entry()])])
    _reconcile_bgp_config(device, payload)
    assert not BGPScope.objects.filter(router__assigned_object_id=device.pk, vrf__isnull=True).exists()
    state = router_model().objects.get(management=management, asn_str="64512")
    assert state.status in {"error", "conflict"}
    assert BGPRouter.objects.filter(pk=state.bgp_router_id).exists()
    document = {
        "routers": [
            entry(
                asn="64512",
                router_id="192.0.2.254",
                scope=[
                    entry(vrf="placeholder-unknown", address_family=[entry(afi="ipv4-unicast")], peer=[], peer_group=[])
                ],
            )
        ],
        "present": ["routers"],
        "unprojectable": [],
    }
    observed = routing_observation("bgp", document=document)
    read_state, _ = NSOFamilyReadState.objects.get_or_create(management=management, family="bgp")
    NSOFamilyObservation.objects.create(read_state=read_state, **observation_defaults("bgp", 1, 1, observed))
    with pytest.raises(OwnershipNotQualified) as raised, transaction.atomic():
        content_update(state, status="accepted")
    assert "VRF" in raised.value.public_message
    state.refresh_from_db()
    assert state.status in {"error", "conflict"}
    assert not NSOOwnershipManifest.objects.filter(device_id=device.pk, scope="bgp").exists()
    assert not BGPScope.objects.filter(router=state.bgp_router, vrf__isnull=True).exists()


def test_router_content_canonicalizes_scope_afs_and_clearing(router_case):
    from netbox_nso_plugin.bgp_router_content import observed_router_content, router_content

    VRF.objects.create(name="placeholder-blue")
    state, _ = import_router(
        router_case,
        router_id=None,
        scopes=[
            scope_entry("placeholder-blue", ("ipv6-unicast", "ipv4-unicast")),
            scope_entry(),
        ],
    )
    content = router_content(state)
    assert content["router_id"] is None
    assert content["scopes"] == [
        {"vrf": "", "address_families": [{"af": "ipv4-unicast"}]},
        {"vrf": "placeholder-blue", "address_families": [{"af": "ipv4-unicast"}, {"af": "ipv6-unicast"}]},
    ]
    entry = copy.deepcopy(
        router_document(
            router_case[0],
            router_id="",
            scopes=[
                scope_entry(),
                scope_entry("placeholder-blue", ("ipv4-unicast", "ipv6-unicast")),
            ],
        )["routers"][0]
    )
    assert observed_router_content(entry) == content


def test_unqualified_owned_router_still_marks_reported_peers_seen(router_case):
    from ipam.models import ASN

    from netbox_nso_plugin.models import NSOBGPPeerState

    _device, management = router_case
    state, _ = import_router(router_case, scopes=[scope_entry(peers=[peer_entry()])])
    peer = NSOBGPPeerState.objects.get(management=management, peer_address_str="192.0.2.1")
    assert peer.bgp_peer_id is not None
    with without_commit_drain():
        state = content_update(state, status="in_sync")
        peer = content_update(peer, status="in_sync")
        router = state.bgp_router
        router.asn = ASN.objects.create(asn=64999, rir=router.asn.rir)
        router.save()
    import_router(router_case, scopes=[scope_entry(peers=[peer_entry()])])
    peer.refresh_from_db()
    assert peer.status == "in_sync"


@pytest.mark.parametrize("unusable_router", [False, True], ids=["usable", "observation-only"])
@pytest.mark.parametrize("status", ["accepted", "in_sync", "imported"])
def test_peer_read_preserves_owned_intent(router_case, status, unusable_router):
    from ipam.models import ASN

    from netbox_nso_plugin.models import NSOBGPPeerState
    from netbox_nso_plugin.status_machine import is_owned

    _device, management = router_case
    state, _ = import_router(router_case, scopes=[scope_entry(peers=[peer_entry()])])
    peer = NSOBGPPeerState.objects.get(management=management, peer_address_str="192.0.2.1")
    with without_commit_drain():
        state = content_update(state, status="accepted")
        peer = content_update(peer, status=status)
        if unusable_router:
            router = state.bgp_router
            router.asn = ASN.objects.create(asn=64999, rir=router.asn.rir)
            router.save()
    observed = {**peer_entry(), "remote_as": "64514", "enabled": False}
    import_router(router_case, scopes=[scope_entry(peers=[observed])])
    peer.refresh_from_db()
    assert (peer.remote_as_str, peer.enabled) == (("64513", True) if is_owned(status) else ("64514", False))
    assert peer.last_sync_at is not None
