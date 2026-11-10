# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Persisted characterization and candidate parity at the delivery seam."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import connections
from django.test import TestCase, TransactionTestCase

from netbox_nso_plugin import delivery
from netbox_nso_plugin.candidate_render import CandidateBatch, bind_candidates, render_rows
from netbox_nso_plugin.models import NSOInterfaceIPState, NSOInterfaceMtuState
from netbox_nso_plugin.renderer_writer import planned_m2m_set

from ._candidate_render_case import persist_candidates, render_candidates
from ._outbox_case import make_managed
from .mixins import IntentPushResetMixin, _CascadeFlushMixin


def payload_bytes(payload):
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()


class TestPersistedRenderCharacterization(IntentPushResetMixin, TestCase):
    def test_populated_persisted_renders_match_characterization(self):
        device, management = make_managed("render-characterization", 1788)
        route_ids = {}
        for index in (2, 1):
            rows, relations = render_candidates(device, management, index)
            persist_candidates(rows, relations)
            route_ids[rows[4].static_route_id] = index
        actual = {}
        for key in delivery.delivery_keys():
            payload = delivery.render(key, device.pk, management.adapter_device_id).payload
            if key == "static_route":
                payload = [{**item, "route_id": route_ids[item["route_id"]]} for item in payload]
            actual[key] = payload_bytes(payload).decode()
        from ._persisted_render_snapshot import PERSISTED_RENDERS

        self.assertEqual(actual, PERSISTED_RENDERS)


RENDER_FAMILIES = (
    "interface",
    "ip",
    "interface_mtu",
    "vlan",
    "static_route",
    "isis",
    "isis_flex_algo",
    "ospf",
    "bgp",
    "route_policy",
    "lacp",
    "switchport",
    "mtu-replacement",
)


@pytest.mark.django_db
@pytest.mark.parametrize("family", RENDER_FAMILIES)
def test_intend_candidate_render_matches_confirmed_render(family):
    device, management = make_managed("candidate-parity", 17881)
    retained, retained_relations = render_candidates(device, management, 2)
    persist_candidates(retained, retained_relations)
    scope = "interface_mtu" if family == "mtu-replacement" else family
    if family == "mtu-replacement":
        candidate = copy.copy(retained[2])
        candidate.pk = None
        candidate.l2_mtu = 9000
        rows, relations = (candidate,), ()
    else:
        rows, relations = render_candidates(device, management, 1)
    before = delivery.render(scope, device.pk, management.adapter_device_id).payload
    rendered = delivery.render(
        scope, device.pk, management.adapter_device_id, candidates=CandidateBatch(rows, relations)
    )
    assert rendered.payload != before, (
        "candidate contribution is missing or the persisted value survived its replacement"
    )
    if family == "mtu-replacement":
        assert rendered.payload == [{"interface_name": "Ethernet2", "mtu": 9000, "ip_mtu": None, "mpls_mtu": None}]
        candidate.pk = retained[2].pk
    persist_candidates(rows, relations)
    confirmed = delivery.render(scope, device.pk, management.adapter_device_id)
    assert payload_bytes(rendered.payload) == payload_bytes(confirmed.payload)


@pytest.mark.django_db
def test_intend_switchport_candidate_renders_planned_tagged_vlans():
    device, management = make_managed("candidate-tagged-vlans", 17882)
    rows, relations = render_candidates(device, management, 1)
    persist_candidates(rows, relations)
    other, _ = render_candidates(device, management, 2)
    candidate = copy.copy(rows[15])
    candidate.pk = None
    related = (other[3].vlan, rows[3].vlan)
    planned = (planned_m2m_set(candidate, "tagged_vlans", related),)
    rendered = delivery.render(
        "switchport", device.pk, management.adapter_device_id, candidates=CandidateBatch((candidate,), planned)
    )
    assert rendered.payload[0]["tagged_vlans"] == [101, 102], "planned VLANs are missing"
    candidate.pk = rows[15].pk
    persist_candidates((candidate,), planned)
    assert payload_bytes(rendered.payload) == payload_bytes(
        delivery.render("switchport", device.pk, management.adapter_device_id).payload
    )


@pytest.mark.django_db
def test_candidate_context_resets_after_success_and_serializer_exception():
    from netbox_nso_plugin.signals import RoutePolicyBindingInvalid

    device, management = make_managed("candidate-context", 17883)
    retained, relations = render_candidates(device, management, 2)
    persist_candidates(retained, relations)
    rows, relations = render_candidates(device, management, 1)
    baseline = delivery.render("route_policy", device.pk, management.adapter_device_id).payload
    batch = CandidateBatch(rows, relations)
    assert len(delivery.render("route_policy", device.pk, management.adapter_device_id, candidates=batch).payload) == 2
    assert delivery.render("route_policy", device.pk, management.adapter_device_id).payload == baseline
    invalid = copy.copy(rows[12])
    invalid.object_name = "wrong-native-name"
    with pytest.raises(RoutePolicyBindingInvalid):
        delivery.render("route_policy", device.pk, management.adapter_device_id, candidates=CandidateBatch((invalid,)))
    assert delivery.render("route_policy", device.pk, management.adapter_device_id).payload == baseline
    from netbox_nso_plugin.models import NSORoutePolicyState

    selected = NSORoutePolicyState.objects.filter(management__device_id=device.pk)
    assert len(render_rows(selected, scope="route_policy")) == 1
    with bind_candidates(batch, device.pk):
        assert delivery.render("route_policy", device.pk, management.adapter_device_id).payload == baseline
        assert len(render_rows(selected, scope="route_policy")) == 2


@pytest.mark.django_db
def test_duplicate_candidates_refuse_even_when_overlay_pks_differ():
    device, management = make_managed("candidate-duplicates", 17884)
    rows, _ = render_candidates(device, management, 1)
    duplicate = copy.copy(rows[2])
    duplicate.pk = 999999
    with pytest.raises(ValueError, match="duplicate candidate identity"):
        delivery.render(
            "interface_mtu", device.pk, management.adapter_device_id, candidates=CandidateBatch((rows[2], duplicate))
        )
    assert delivery.render("interface_mtu", device.pk, management.adapter_device_id).payload == []


@pytest.mark.django_db
def test_candidate_order_is_native_order_and_ignores_batch_order():
    device, management = make_managed("candidate-order", 17885)
    first, first_relations = render_candidates(device, management, 10)
    second, second_relations = render_candidates(device, management, 2)
    batch = CandidateBatch((*first, *second), (*first_relations, *second_relations))
    reverse = CandidateBatch(tuple(reversed(batch.overlays)), tuple(reversed(batch.m2m)))
    for scope in (
        "interface",
        "ip",
        "interface_mtu",
        "vlan",
        "static_route",
        "isis",
        "bgp",
        "ospf",
        "switchport",
        "route_policy",
    ):
        rendered = delivery.render(scope, device.pk, management.adapter_device_id, candidates=batch)
        assert (
            rendered.payload
            == delivery.render(scope, device.pk, management.adapter_device_id, candidates=reverse).payload
        )
    assert [
        row["interface_name"]
        for row in delivery.render("interface_mtu", device.pk, management.adapter_device_id, candidates=batch).payload
    ] == ["Ethernet2", "Ethernet10"]
    persisted = {
        scope: delivery.render(scope, device.pk, management.adapter_device_id, candidates=batch).payload
        for scope in delivery.delivery_keys()
    }
    persist_candidates(batch.overlays, batch.m2m)
    assert persisted == {
        scope: delivery.render(scope, device.pk, management.adapter_device_id).payload
        for scope in delivery.delivery_keys()
    }


@pytest.mark.django_db
def test_candidate_policy_names_follow_database_collation():
    from django.contrib.contenttypes.models import ContentType
    from netbox_routing.models import PrefixList

    from netbox_nso_plugin.models import NSORoutePolicyState

    device, management = make_managed("candidate-policy-collation", 17896)
    rows = []
    for name in ("alpha", "Beta", "beta", "Alpha"):
        policy = PrefixList.objects.create(name=name, family=4)
        rows.append(
            NSORoutePolicyState(
                management=management,
                family="prefix_list",
                object_name=name,
                content_type=ContentType.objects.get_for_model(PrefixList),
                object_id=policy.pk,
                status="accepted",
            )
        )
    persist_candidates(rows[:1])
    rendered = delivery.render(
        "route_policy", device.pk, management.adapter_device_id, candidates=CandidateBatch(tuple(rows[1:]))
    )
    persist_candidates(rows[1:])
    confirmed = delivery.render("route_policy", device.pk, management.adapter_device_id)
    assert payload_bytes(rendered.payload) == payload_bytes(confirmed.payload)


@pytest.mark.django_db
@pytest.mark.parametrize("ordering", ("process_id", "-process_id"))
def test_candidate_text_order_matches_database_with_nulls(ordering):
    from dcim.models import Interface

    from netbox_nso_plugin.models import NSOOSPFInterfaceState

    device, management = make_managed("candidate-null-order", 17897)
    rows = tuple(
        NSOOSPFInterfaceState(
            management=management,
            interface=Interface.objects.create(device=device, name=f"Ethernet{index}", type="1000base-t"),
            process_id=name,
            status="accepted",
        )
        for index, name in enumerate(("alpha", "Beta", "beta", "Alpha", None), start=1)
    )
    selected = NSOOSPFInterfaceState.objects.filter(management=management).order_by(ordering)
    with bind_candidates(CandidateBatch(rows), device.pk):
        candidate_order = tuple(row.process_id for row in render_rows(selected, scope="ospf"))
    persist_candidates(rows)
    assert candidate_order == tuple(row.process_id for row in render_rows(selected, scope="ospf"))
    assert candidate_order[0 if ordering.startswith("-") else -1] is None


@pytest.mark.django_db
def test_candidate_ownership_filter_runs_after_replacement():
    device, management = make_managed("candidate-ownership", 17886)
    rows, relations = render_candidates(device, management, 1)
    persist_candidates(rows, relations)
    candidate = copy.copy(rows[2])
    candidate.pk = None
    candidate.status = "imported"
    assert (
        delivery.render(
            "interface_mtu", device.pk, management.adapter_device_id, candidates=CandidateBatch((candidate,))
        ).payload
        == []
    )
    assert delivery.render("interface_mtu", device.pk, management.adapter_device_id).payload[0]["mtu"] == 1500


@pytest.mark.django_db
def test_candidate_ip_identity_keeps_equal_addresses_in_different_vrfs():
    device, management = make_managed("candidate-vrf", 17887)
    rows, _ = render_candidates(device, management, 1)
    first = rows[1]
    second = copy.copy(first)
    second.vrf = "other-vrf"
    batch = CandidateBatch((first, second))
    payload = delivery.render("ip", device.pk, management.adapter_device_id, candidates=batch).payload
    assert [row["vrf"] for row in payload] == ["", "other-vrf"]
    assert not NSOInterfaceIPState.objects.filter(interface=first.interface).exists()


@pytest.mark.django_db
def test_candidate_replaces_an_unowned_row_by_native_identity():
    device, management = make_managed("candidate-unowned", 17890)
    rows, _ = render_candidates(device, management, 1)
    current = rows[2]
    current.status = "imported"
    persist_candidates((current,))
    candidate = copy.copy(current)
    candidate.pk = None
    candidate.status = "accepted"
    candidate.l2_mtu = 9000
    rendered = delivery.render(
        "interface_mtu", device.pk, management.adapter_device_id, candidates=CandidateBatch((candidate,))
    )
    assert rendered.payload == [{"interface_name": "Ethernet1", "mtu": 9000, "ip_mtu": None, "mpls_mtu": None}]
    candidate.pk = current.pk
    persist_candidates((candidate,))
    assert rendered.payload == delivery.render("interface_mtu", device.pk, management.adapter_device_id).payload


@pytest.mark.django_db
def test_candidate_batch_does_not_cross_device_scope():
    first_device, first_management = make_managed("candidate-device-first", 17891)
    second_device, second_management = make_managed("candidate-device-second", 17892)
    first, _ = render_candidates(first_device, first_management, 1)
    second, _ = render_candidates(second_device, second_management, 2)
    batch = CandidateBatch((first[2], second[2]))
    for device, management, name in (
        (first_device, first_management, "Ethernet1"),
        (second_device, second_management, "Ethernet2"),
    ):
        payload = delivery.render("interface_mtu", device.pk, management.adapter_device_id, candidates=batch).payload
        assert [row["interface_name"] for row in payload] == [name]


@pytest.mark.django_db
def test_switchport_candidate_without_planned_m2m_reads_the_stable_existing_relation():
    device, management = make_managed("candidate-existing-relation", 17893)
    rows, relations = render_candidates(device, management, 1)
    persist_candidates(rows, relations)
    candidate = copy.copy(rows[15])
    candidate.pk = None
    payload = delivery.render(
        "switchport", device.pk, management.adapter_device_id, candidates=CandidateBatch((candidate,))
    ).payload
    assert payload[0]["tagged_vlans"] == [101]
    cleared = CandidateBatch((candidate,), (planned_m2m_set(candidate, "tagged_vlans", ()),))
    assert (
        delivery.render("switchport", device.pk, management.adapter_device_id, candidates=cleared).payload[0][
            "tagged_vlans"
        ]
        == []
    )


@pytest.mark.django_db
def test_candidate_lacp_qualification_runs_after_replacement():
    device, management = make_managed("candidate-lacp-qualification", 17894)
    rows, relations = render_candidates(device, management, 1)
    assert (
        delivery.render("lacp", device.pk, management.adapter_device_id, candidates=CandidateBatch((rows[14],))).payload
        == []
    )
    persist_candidates(rows, relations)
    candidate = copy.copy(rows[13])
    candidate.pk = None
    candidate.vpc_sensitive = True
    assert (
        delivery.render(
            "lacp", device.pk, management.adapter_device_id, candidates=CandidateBatch((candidate,))
        ).payload
        == []
    )
    assert len(delivery.render("lacp", device.pk, management.adapter_device_id).payload) == 1


@pytest.mark.django_db
def test_candidate_bgp_child_requires_a_renderable_owned_router():
    device, management = make_managed("candidate-bgp-qualification", 17895)
    rows, _ = render_candidates(device, management, 1)
    for child in (rows[11], rows[-1]):
        payload = delivery.render(
            "bgp", device.pk, management.adapter_device_id, candidates=CandidateBatch((child,))
        ).payload
        assert payload["blocked"]["detail"]["reason"] == "blocked_bgp_router"


class TestCandidateThreadIsolation(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def test_candidate_context_is_isolated_between_threads(self):
        device, management = make_managed("candidate-threads", 17888)
        rows, relations = render_candidates(device, management, 1)
        persist_candidates(rows, relations)
        candidate = copy.copy(rows[2])
        candidate.pk = None
        candidate.l2_mtu = 9000
        batch = CandidateBatch((candidate,))
        barrier = Barrier(2)

        def read(candidate_batch):
            try:
                with bind_candidates(candidate_batch, device.pk):
                    barrier.wait(timeout=15)
                    selected = render_rows(
                        NSOInterfaceMtuState.objects.filter(management__device_id=device.pk), scope="interface_mtu"
                    )
                    return tuple(row.l2_mtu for row in selected)
            finally:
                connections.close_all()

        with bind_candidates(batch, device.pk), ThreadPoolExecutor(max_workers=2) as pool:
            candidate_result = pool.submit(read, batch)
            persisted_result = pool.submit(read, None)
            assert candidate_result.result(timeout=30) == (9000,)
            assert persisted_result.result(timeout=30) == (1500,)
            selected = render_rows(
                NSOInterfaceMtuState.objects.filter(management__device_id=device.pk), scope="interface_mtu"
            )
            assert selected[0].l2_mtu == 9000
        assert delivery.render("interface_mtu", device.pk, management.adapter_device_id).payload[0]["mtu"] == 1500


@pytest.mark.django_db
def test_bgp_candidate_redistribution_survives_unsaved_observation_rows():
    from netbox_nso_plugin.models import NSORedistributionState

    device, management = make_managed("candidate-bgp-observation", 17889)
    retained, relations = render_candidates(device, management, 2)
    persist_candidates(retained, relations)
    rows, _ = render_candidates(device, management, 1)
    observation = NSORedistributionState(
        management=management, dest_protocol="bgp", dest_ref="64514/", source_protocol="static", status="accepted"
    )
    batch = CandidateBatch((rows[10], rows[-1], observation))
    payload = delivery.render("bgp", device.pk, management.adapter_device_id, candidates=batch).payload
    assert payload[0]["scopes"][0]["address_families"][0]["redistribution"] == [
        {"source_protocol": "connected", "source_ref": "", "metric": 1}
    ]
