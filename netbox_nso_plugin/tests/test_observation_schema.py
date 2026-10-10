# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Refuse malformed observations before any family publication."""

import copy

import pytest

from netbox_nso_plugin.observations import ObservationProtocolError, observation_defaults

from ._routing_observation_case import DOCUMENTS as ROUTING_DOCUMENTS
from ._routing_observation_case import routing_observation
from ._scope_observation_case import DOCUMENTS, scope_observation

DOCUMENTS = {**DOCUMENTS, **ROUTING_DOCUMENTS}


def family_observation(family, **kwargs):
    if family in ROUTING_DOCUMENTS:
        return routing_observation(family, **kwargs)
    return scope_observation(family, **kwargs)


@pytest.mark.parametrize("family", DOCUMENTS)
def test_valid_family_document_is_copied(family):
    observed = family_observation(family)
    snapshot = observation_defaults(family, 1, 1, observed)
    assert snapshot["document"] == observed["document"]
    assert snapshot["coverage"] == observed["coverage"]
    observed["document"].clear()
    assert snapshot["document"] == DOCUMENTS[family]


@pytest.mark.parametrize("family", DOCUMENTS)
@pytest.mark.parametrize("mutation", ["extra", "missing", "wrong_type"])
def test_invalid_family_document_is_refused(family, mutation):
    document = copy.deepcopy(DOCUMENTS[family])
    if mutation == "extra":
        document["unexpected"] = False
    elif mutation == "missing":
        del document["unprojectable"]
    else:
        document["unprojectable"] = "invalid"
    with pytest.raises(ObservationProtocolError):
        observation_defaults(family, 1, 1, family_observation(family, document=document))


@pytest.mark.parametrize(
    "field,value",
    [
        ("family", "bfd"),
        ("revision", True),
        ("source_epoch", 2),
        ("digest", "0" * 64),
        ("observed_at", "2026-10-01T12:00:00"),
    ],
)
def test_publication_envelope_remains_bound_to_payload(field, value):
    observed = scope_observation("interface_attributes")
    observed[field] = value
    with pytest.raises(ObservationProtocolError):
        observation_defaults("interface_attributes", 1, 1, observed)


@pytest.mark.parametrize("family", DOCUMENTS)
@pytest.mark.parametrize("mutation", ["extra", "wrong_type"])
def test_nested_entry_schema_is_enforced(family, mutation):
    document = copy.deepcopy(DOCUMENTS[family])
    collection = next(
        value
        for key, value in document.items()
        if key != "unprojectable" and isinstance(value, list) and value and isinstance(value[0], dict)
    )
    if mutation == "extra":
        collection[0]["unexpected"] = "invalid"
    else:
        name = next(name for name in collection[0] if name != "present")
        collection[0][name] = []
    with pytest.raises(ObservationProtocolError):
        observation_defaults(family, 1, 1, family_observation(family, document=document))


@pytest.mark.parametrize("family", ["bgp", "isis", "ospf"])
def test_routing_credentials_are_presence_only(family):
    document = copy.deepcopy(DOCUMENTS[family])
    if family == "bgp":
        document["routers"][0]["scope"][0]["peer"][0]["password"] = "placeholder-secret"
    elif family == "isis":
        document["processes"][0]["area_auth_key"] = "placeholder-secret"
    else:
        document["interfaces"][0]["auth_key"] = "placeholder-secret"
    with pytest.raises(ObservationProtocolError):
        observation_defaults(family, 1, 1, routing_observation(family, document=document))


def test_redistribution_requires_per_component_coverage():
    observed = routing_observation("redistribution")
    del observed["coverage"]["components"]
    with pytest.raises(ObservationProtocolError):
        observation_defaults("redistribution", 1, 1, observed)


def test_every_read_family_has_an_observation_schema():
    from netbox_nso_plugin.families import ALL_FAMILY_KEYS
    from netbox_nso_plugin.observations import OBSERVATION_SCHEMAS

    assert set(OBSERVATION_SCHEMAS) == set(ALL_FAMILY_KEYS)
