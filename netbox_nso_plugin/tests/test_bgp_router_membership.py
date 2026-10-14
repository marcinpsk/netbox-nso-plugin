# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Read cleanup removes only unowned protected BGP graph dependencies."""

import pytest
from netbox_routing.models import BGPAddressFamily, BGPPeer, BGPPeerAddressFamily, BGPPeerTemplate, BGPScope

from netbox_nso_plugin.bgp_reconciler import _reconcile_bgp_config

from ._outbox_case import content_update, make_managed, without_commit_drain
from .test_bgp_router_reconcile import import_router, peer_entry, router_document, scope_entry


@pytest.fixture
def membership_case(db):
    return make_managed("router-membership", 1767)


@pytest.mark.parametrize("scopes", [[], [scope_entry(afs=())]])
def test_router_read_preserves_empty_unowned_scope_or_af_as_conflict(membership_case, scopes):
    from netbox_nso_plugin.bgp_router_content import router_content

    state, _ = import_router(membership_case)
    scope_id = BGPScope.objects.get(router=state.bgp_router).pk
    af_id = BGPAddressFamily.objects.get(scope_id=scope_id).pk
    before = router_content(state)
    base_hash = state.device_base_hash

    result = _reconcile_bgp_config(membership_case[0], router_document(membership_case[0], scopes=scopes))

    assert result == []
    state.refresh_from_db()
    assert state.status == "conflict"
    assert BGPAddressFamily.objects.filter(pk=af_id).exists()
    assert BGPScope.objects.filter(pk=scope_id).exists()
    assert router_content(state) == before
    assert state.device_base_hash == base_hash


@pytest.mark.parametrize("scopes", [[], [scope_entry(afs=())]])
def test_router_read_preserves_unowned_peer_and_template_af_dependencies(membership_case, scopes):
    initial = scope_entry(peers=[peer_entry()])
    initial["peer_groups"] = [
        {
            "name": "placeholder-group",
            "remote_as": "64513",
            "address_families": [{"af": "ipv4-unicast", "enabled": True}],
        }
    ]
    state, peers = import_router(membership_case, scopes=[initial])
    peer = peers[0]
    native_peer_id = peer.bgp_peer_id
    scope_id = peer.bgp_peer.scope_id
    family_id = BGPAddressFamily.objects.get(scope_id=scope_id).pk
    link_ids = set(BGPPeerAddressFamily.objects.filter(address_family_id=family_id).values_list("pk", flat=True))
    assert len(link_ids) == 2

    _reconcile_bgp_config(membership_case[0], router_document(membership_case[0], scopes=scopes))

    peer.refresh_from_db()
    assert peer.status == "changed"
    assert set(BGPPeerAddressFamily.objects.filter(pk__in=link_ids).values_list("pk", flat=True)) == link_ids
    assert BGPAddressFamily.objects.filter(pk=family_id).exists()
    assert BGPScope.objects.filter(pk=scope_id).exists()
    assert BGPPeer.objects.filter(pk=native_peer_id).exists()
    assert BGPPeerTemplate.objects.filter(name="placeholder-group").exists()
    assert peer.bgp_peer_id == native_peer_id
    state.refresh_from_db()
    assert state.status == "conflict"


def test_router_read_mixed_membership_removal_and_addition_is_atomic_and_repeatable(membership_case):
    from netbox_nso_plugin.bgp_router_content import router_content

    state, _ = import_router(membership_case)
    before = router_content(state)
    base_hash = state.device_base_hash
    payload = router_document(membership_case[0], router_id="192.0.2.253", scopes=[scope_entry(afs=("ipv6-unicast",))])

    for _ in range(2):
        _reconcile_bgp_config(membership_case[0], payload)
        state.refresh_from_db()
        assert state.status == "conflict"
        assert state.router_id == "192.0.2.253"
        assert router_content(state) == before
        assert state.device_base_hash == base_hash


@pytest.mark.parametrize(
    "gap", ["nested_scope", "nested_af", "router_id", "top_routers", "unprojectable", "attributes"]
)
def test_incomplete_bgp_observation_preserves_membership_and_confirmed_lifecycle(membership_case, gap):
    from netbox_nso_plugin.bgp_reconciler import bgp_reconcile_plan
    from netbox_nso_plugin.bgp_router_content import router_content
    from netbox_nso_plugin.models import NSOFamilyObservation
    from netbox_nso_plugin.read_gate import gated_family_run

    from ._routing_observation_case import routing_observation
    from ._scope_observation_case import entry
    from .test_bgp_router_reconcile import router_model
    from .test_read_gate import _rs

    device, management = membership_case

    def publish(payload, document, revision, coverage=None):
        observed = routing_observation("bgp", document=document, revision=revision, coverage=coverage)
        payload["observation"] = observed
        result = gated_family_run(
            management,
            "bgp",
            _rs(attempt_id=revision),
            lambda: _reconcile_bgp_config(device, payload),
            epoch=management.adapter_device_id,
            pre_body=lambda: bgp_reconcile_plan(device, payload),
            observation=observed,
        )
        assert result.disposition == "ran"

    complete_scope = entry(
        vrf="",
        address_family=[entry(afi="ipv4-unicast")],
        peer=[],
        peer_group=[],
    )
    document = {
        "routers": [entry(asn="64512", router_id="192.0.2.254", scope=[complete_scope])],
        "present": ["routers"],
        "unprojectable": [],
    }
    publish(router_document(device), document, 1)
    state = router_model().objects.get(management=management)
    if gap not in {"nested_scope", "nested_af", "router_id"}:
        with without_commit_drain():
            state = content_update(state, status="in_sync")
    before_status = state.status
    before_content = router_content(state)
    before_hash = state.device_base_hash
    coverage = None
    payload = router_document(device, scopes=[])
    if gap == "nested_scope":
        document["routers"] = [entry(asn="64512", router_id="192.0.2.254")]
    elif gap == "nested_af":
        document["routers"] = [
            entry(asn="64512", router_id="192.0.2.254", scope=[entry(vrf="", peer=[], peer_group=[])])
        ]
        payload = router_document(device, scopes=[scope_entry(afs=())])
    elif gap == "router_id":
        document["routers"] = [entry(asn="64512", scope=[complete_scope])]
        payload = router_document(device, router_id=None)
    else:
        payload["routers"] = []
        document["routers"] = None if gap == "top_routers" else []
        if gap == "top_routers":
            document["present"] = []
        elif gap == "unprojectable":
            document["unprojectable"] = [{"index": 0, "reason": "placeholder unavailable router"}]
        else:
            coverage = {"attributes": ["asn", "router_id"], "not_comparable": []}

    publish(payload, document, 2, coverage)

    state.refresh_from_db()
    assert state.status == before_status
    assert router_content(state) == before_content
    assert state.device_base_hash == before_hash
    observed = NSOFamilyObservation.objects.get(read_state__management=management, read_state__family="bgp")
    assert observed.document == document
    assert observed.revision == observed.read_state.applied_payload_revision == 2
    if gap == "router_id":
        document["routers"] = [entry(asn="64512", router_id=None, scope=[complete_scope])]
        publish(router_document(device, router_id=None), document, 3)
        state.refresh_from_db()
        assert state.status == "imported"
        assert router_content(state)["router_id"] is None
        assert state.device_base_hash != before_hash


def test_repeated_unbased_router_read_preserves_divergent_native_content(membership_case):
    from dcim.models import Device
    from django.contrib.contenttypes.models import ContentType
    from ipam.models import ASN, RIR
    from netbox_routing.models import BGPRouter

    from netbox_nso_plugin.bgp_router_content import router_content

    from .test_bgp_router_reconcile import router_model

    device, management = membership_case
    rir = RIR.objects.create(name="placeholder-router-rir", slug="placeholder-router-rir")
    asn = ASN.objects.create(asn=64512, rir=rir)
    router = BGPRouter.objects.create(
        assigned_object_type=ContentType.objects.get_for_model(Device),
        assigned_object_id=device.pk,
        asn=asn,
        name="placeholder-router",
        router_id="192.0.2.254",
    )
    scope = BGPScope.objects.create(router=router)
    BGPAddressFamily.objects.create(scope=scope, address_family="ipv4-unicast")
    before = router_content(router)
    payload = router_document(device, router_id="192.0.2.253")

    for _ in range(2):
        _reconcile_bgp_config(device, payload)
        state = router_model().objects.get(management=management, asn_str="64512")
        router.refresh_from_db()
        assert state.status == "conflict"
        assert state.router_id == "192.0.2.253"
        assert state.device_base_hash == ""
        assert router_content(router) == before


@pytest.mark.parametrize("scopes", [[], [scope_entry(afs=())]])
def test_router_read_preserves_independently_owned_peer_graph(membership_case, scopes):
    from netbox_nso_plugin.bgp_router_content import router_content, router_content_digest

    state, peers = import_router(membership_case, scopes=[scope_entry(peers=[peer_entry()])])
    with without_commit_drain():
        peer = content_update(peers[0], status="accepted")
    before = router_content(state)
    digest = router_content_digest(before)
    peer_id = peer.bgp_peer_id
    link_ids = set(peer.bgp_peer.address_families.values_list("pk", flat=True))

    _reconcile_bgp_config(membership_case[0], router_document(membership_case[0], scopes=scopes))

    state.refresh_from_db()
    peer.refresh_from_db()
    assert state.status == "conflict"
    assert peer.status == "accepted"
    assert peer.bgp_peer_id == peer_id
    assert BGPPeer.objects.filter(pk=peer_id).exists()
    assert set(peer.bgp_peer.address_families.values_list("pk", flat=True)) == link_ids
    assert router_content(state) == before
    assert router_content_digest(router_content(state)) == digest
