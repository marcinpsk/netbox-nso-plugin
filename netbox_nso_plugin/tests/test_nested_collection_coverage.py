# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Unknown collections cannot replace a certified native graph."""

import copy

import pytest
from netbox_routing.models import ISISLevel

from netbox_nso_plugin.adapter_client import AdapterError
from netbox_nso_plugin.isis_reconciler import reconcile_isis

from ._outbox_case import make_managed
from ._routing_observation_case import routing_observation
from ._scope_observation_case import entry


@pytest.fixture
def managed_device(db):
    return make_managed("collection-coverage", 1833)


def isis_payload(*, levels):
    process = entry(
        process_tag="CORE",
        setting=[],
        level=levels,
        flex_algo=[],
        srv6_locator=[],
        segment_routing=None,
        segment_routing_reported=True,
        segment_routing_configured=False,
    )
    mirror = {"process_tag": "CORE", "segment_routing_reported": True, "segment_routing_configured": False}
    if levels:
        mirror["levels"] = [{key: value for key, value in level.items() if key != "present"} for level in levels]
    return {
        "processes": [mirror],
        "interfaces": [],
        "observation": routing_observation(
            "isis",
            document={
                "processes": [process],
                "interfaces": [],
                "present": ["processes", "interfaces"],
                "unprojectable": [],
            },
        ),
    }


@pytest.mark.parametrize("unknown", ["unmarked", "null", "missing", "no_present"])
def test_isis_unknown_levels_refuse_reconcile_without_changing_native_rows(managed_device, unknown):
    device, _management = managed_device
    seed = isis_payload(levels=[entry(level=2, default_metric=10)])
    reconcile_isis(device, seed)
    level = ISISLevel.objects.get(instance__device=device)
    payload = isis_payload(levels=[])
    process = payload["observation"]["document"]["processes"][0]
    if unknown == "null":
        process["level"] = None
    elif unknown == "missing":
        del process["level"]
    elif unknown == "no_present":
        del process["present"]
    else:
        process["present"].remove("level")
    before = copy.deepcopy(level.__dict__)
    with pytest.raises(AdapterError, match="coverage"):
        reconcile_isis(device, payload)
    level.refresh_from_db()
    assert level.default_metric == before["default_metric"]
    assert ISISLevel.objects.filter(pk=level.pk).exists()


def test_isis_covered_empty_levels_delete_native_rows_when_mirror_omits_levels(managed_device):
    device, _management = managed_device
    reconcile_isis(device, isis_payload(levels=[entry(level=2, default_metric=10)]))
    assert ISISLevel.objects.filter(instance__device=device).exists()
    reconcile_isis(device, isis_payload(levels=[]))
    assert not ISISLevel.objects.filter(instance__device=device).exists()
