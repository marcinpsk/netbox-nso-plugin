# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Validate the observation wire document without a NetBox runtime."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def _validator():
    path = Path(__file__).resolve().parents[1] / "observations.py"
    spec = importlib.util.spec_from_file_location("observations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _observation():
    return {
        "family": "interface_attributes",
        "revision": 3,
        "source_epoch": 2,
        "digest": "52155b66596db46cc9f022b1889b53ee59f70c0a0e619c6e0e46c735fa908b95",
        "observed_at": "2026-10-01T12:00:00+00:00",
        "coverage": {"attributes": ["description", "enabled"]},
        "document": {"interfaces": [], "unprojectable": []},
    }


def test_authoritative_empty_is_valid_and_has_an_aware_timestamp():
    validator = _validator()
    defaults = validator.observation_defaults("interface_attributes", 3, 2, _observation())
    assert defaults["document"] == {"interfaces": [], "unprojectable": []}
    assert defaults["observed_at"].utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", 4),
        ("source_epoch", 3),
        ("family", "interface_ip"),
        ("digest", "0" * 64),
        ("digest", "A" * 64),
        ("observed_at", "2026-10-01T12:00:00"),
        ("observed_at", "invalid"),
        ("coverage", {"attributes": [False]}),
        ("revision", True),
        ("document", None),
        ("document", {"interfaces": [{"name": None}], "unprojectable": []}),
        ("document", {"interfaces": [], "unprojectable": [{"index": True, "reason": "missing name"}]}),
    ],
)
def test_invalid_observation_is_a_protocol_error(field, value):
    validator = _validator()
    snapshot = _observation()
    snapshot[field] = value
    with pytest.raises(validator.ObservationProtocolError):
        validator.observation_defaults("interface_attributes", 3, 2, snapshot)


def test_snapshot_preserves_values_and_does_not_share_mutable_payload():
    validator = _validator()
    snapshot = _observation()
    item = {
        "name": "Ethernet1",
        "description": "",
        "enabled": False,
        "kind": None,
        "parent_binding": None,
        "encap_tag": "0",
        "vrf": None,
        "service": None,
    }
    snapshot["document"]["interfaces"] = [copy.deepcopy(item)]
    snapshot["digest"] = hashlib.sha256(
        json.dumps(snapshot["document"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    defaults = validator.observation_defaults("interface_attributes", 3, 2, snapshot)
    snapshot["document"]["interfaces"][0]["description"] = "later mutation"
    assert defaults["document"]["interfaces"] == [item]


@pytest.mark.parametrize("observation", [None, {}])
def test_missing_observation_is_a_protocol_error(observation):
    validator = _validator()
    with pytest.raises(validator.ObservationProtocolError):
        validator.observation_defaults("interface_attributes", 3, 2, observation)


def test_ip_authoritative_empty_uses_the_same_canonical_digest():
    validator = _validator()
    snapshot = _observation()
    snapshot["family"] = "interface_ip"
    snapshot["coverage"] = {"attributes": ["address", "prefix_length", "secondary", "vrf"]}
    defaults = validator.observation_defaults("interface_ip", 3, 2, snapshot)
    assert defaults["digest"] == "52155b66596db46cc9f022b1889b53ee59f70c0a0e619c6e0e46c735fa908b95"


@pytest.mark.parametrize("field", ["revision", "source_epoch"])
def test_boolean_read_identity_is_a_protocol_error(field):
    validator = _validator()
    snapshot = _observation()
    snapshot[field] = 1
    revision = True if field == "revision" else 3
    source_epoch = True if field == "source_epoch" else 2
    with pytest.raises(validator.ObservationProtocolError):
        validator.observation_defaults("interface_attributes", revision, source_epoch, snapshot)
