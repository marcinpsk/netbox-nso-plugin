# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep refused and empty persisted renders at their original query counts."""

import copy

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from netbox_nso_plugin import delivery
from netbox_nso_plugin.candidate_render import CandidateBatch

from ._candidate_render_case import ACCEPTED_AT, persist_candidates, render_candidates
from ._outbox_case import make_managed


@pytest.mark.django_db
def test_blocked_bgp_router_keeps_persisted_query_count():
    device, management = make_managed("render-blocked-bgp-queries", 17898)
    rows, _ = render_candidates(device, management, 1)
    router, peer = rows[10:12]
    persist_candidates((router, peer))
    router.bgp_router = None
    persist_candidates((router,))

    with CaptureQueriesContext(connection) as queries:
        rendered = delivery.render("bgp", device.pk, management.adapter_device_id)

    assert rendered.payload["blocked"]["detail"] == {
        "reason": "blocked_bgp_router",
        "asn": router.asn_str,
        "state_id": router.pk,
    }
    assert len(queries) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("vpc_only", (False, True), ids=("no-bundles", "vpc-only"))
def test_lacp_without_qualifying_bundles_keeps_persisted_query_count(vpc_only):
    from dcim.models import Interface

    from netbox_nso_plugin.models import NSOLACPBundleState

    device, management = make_managed("render-empty-lacp-queries", 17899)
    if vpc_only:
        bundle = NSOLACPBundleState(
            management=management,
            interface=Interface.objects.create(device=device, name="Port-channel1", type="lag"),
            lag_id=1,
            status="accepted",
            accepted_at=ACCEPTED_AT,
        )
        persist_candidates((bundle,))
        bundle.vpc_sensitive = True
        persist_candidates((bundle,))

    with CaptureQueriesContext(connection) as queries:
        rendered = delivery.render("lacp", device.pk, management.adapter_device_id)

    assert rendered.payload["bundles"] == []
    assert len(queries) == 1


def _candidate_render_query_counts(name, asn, scope, candidate_index):
    """Count one candidate render with one and with two persisted graphs."""
    device, management = make_managed(name, asn)
    counts = []
    for index in (1, 2):
        rows, relations = render_candidates(device, management, index)
        persist_candidates(rows, relations)
        if index == 1:
            candidate = copy.copy(rows[candidate_index])
            candidate.pk = None
        with CaptureQueriesContext(connection) as queries:
            delivery.render(scope, device.pk, management.adapter_device_id, candidates=CandidateBatch((candidate,)))
        counts.append(len(queries))
    return counts


@pytest.mark.django_db
def test_candidate_render_does_not_load_management_per_persisted_row():
    first, second = _candidate_render_query_counts("render-candidate-vlan-queries", 17900, "vlan", 3)
    assert second == first, "each persisted row loads its identity relation"


@pytest.mark.django_db
def test_related_render_without_a_matching_candidate_does_not_load_management_per_row():
    first, second = _candidate_render_query_counts("render-related-switchport-queries", 17901, "switchport", 3)
    assert second == first, "each persisted row loads its identity relation or its tagged VLANs"


@pytest.mark.django_db
def test_persisted_switchport_render_reads_tagged_vlans_once():
    device, management = make_managed("render-persisted-switchport-queries", 17902)
    counts = []
    for index in (1, 2):
        persist_candidates(*render_candidates(device, management, index))
        with CaptureQueriesContext(connection) as queries:
            delivery.render("switchport", device.pk, management.adapter_device_id)
        counts.append(len(queries))
    assert counts[1] == counts[0], "each persisted switchport reads its tagged VLANs"
