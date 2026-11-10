# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Keep refused and empty persisted renders at their original query counts."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from netbox_nso_plugin import delivery

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
