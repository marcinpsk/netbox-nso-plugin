# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""IS-IS coverage admission preserves every native child collection."""

import copy

import pytest
from dcim.models import Interface

from netbox_nso_plugin.adapter_client import AdapterError
from netbox_nso_plugin.isis_reconciler import reconcile_isis
from netbox_nso_plugin.models import NSOISISInstanceState, NSOISISInterfaceState

from ._outbox_case import make_managed
from ._scope_observation_case import entry
from .test_nested_collection_coverage import isis_payload


@pytest.mark.parametrize(
    "kind,collection",
    [
        ("processes", "setting"),
        ("processes", "flex_algo"),
        ("processes", "srv6_locator"),
        ("processes", "segment_routing"),
        ("interfaces", "setting"),
        ("interfaces", "level"),
        ("interfaces", "prefix_sid"),
    ],
)
@pytest.mark.parametrize("unknown", ["unmarked", "null"])
def test_unknown_isis_child_keeps_native_content_and_lifecycle(db, kind, collection, unknown):
    from netbox_routing.models import (
        ISISFlexAlgo,
        ISISInterfaceLevel,
        ISISLevel,
        ISISPrefixSID,
        ISISSegmentRouting,
        ISISSetting,
        ISISSRv6Locator,
    )

    device, management = make_managed("isis-collection", 1833)
    Interface.objects.create(device=device, name="Ethernet1", type="1000base-t")
    seed = isis_payload(levels=[entry(level=2, default_metric=10)])
    process = seed["processes"][0]
    process.update(
        settings={"spf_second_wait": "1000"},
        flex_algos=[{"algo_id": 128, "metric_type": "igp-metric"}],
        srv6_locators=[{"name": "EXAMPLE", "prefix": "2001:db8:1::/64"}],
        segment_routing={"enabled": True},
        segment_routing_configured=True,
    )
    observed = seed["observation"]["document"]["processes"][0]
    observed.update(
        setting=[entry(key="spf_second_wait", value="1000")],
        flex_algo=[entry(algo_id=128, metric_type="igp-metric")],
        srv6_locator=[entry(name="EXAMPLE", prefix="2001:db8:1::/64")],
        segment_routing=entry(enabled=True),
        segment_routing_configured=True,
    )
    seed["interfaces"] = [
        {
            "interface_name": "Ethernet1",
            "af": "ipv4",
            "process_tag": "CORE",
            "settings": {"hello_padding": "true"},
            "levels": [{"level": 2, "metric": 20}],
            "prefix_sids": [{"algorithm": 0, "sid_index": 101}],
        }
    ]
    seed["observation"]["document"]["interfaces"] = [
        entry(
            interface_name="Ethernet1",
            af="ipv4",
            process_tag="CORE",
            setting=[entry(key="hello_padding", value="true")],
            level=[entry(level=2, metric=20)],
            prefix_sid=[entry(algorithm=0, sid_index=101)],
        )
    ]
    reconcile_isis(device, seed)
    models = (
        ISISSetting,
        ISISLevel,
        ISISFlexAlgo,
        ISISSRv6Locator,
        ISISSegmentRouting,
        ISISInterfaceLevel,
        ISISPrefixSID,
    )
    before = {model: list(model.objects.order_by("pk").values()) for model in models}
    assert all(before.values())
    states = (NSOISISInstanceState, NSOISISInterfaceState)
    lifecycle = {model: list(model.objects.filter(management=management).values()) for model in states}
    payload = copy.deepcopy(seed)
    observed = payload["observation"]["document"][kind][0]
    if unknown == "unmarked":
        observed["present"].remove(collection)
    else:
        observed[collection] = None
    with pytest.raises(AdapterError, match="coverage"):
        reconcile_isis(device, payload)
    assert {model: list(model.objects.order_by("pk").values()) for model in models} == before
    assert {model: list(model.objects.filter(management=management).values()) for model in states} == lifecycle
