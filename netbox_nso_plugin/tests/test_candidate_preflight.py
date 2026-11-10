# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Candidate admission evidence through the real HTTP client boundary."""

from copy import deepcopy

import pytest

from netbox_nso_plugin import adapter_client, apply_preflight

from ._adapter_http import make_apply_preflight, make_response
from ._outbox_case import CFG, ReceiptAdapter


def candidate_wire(*, status="ready", promotes=False):
    wire = make_apply_preflight({"vlan": 41, "ip": 42}, status=status)
    wire.pop("preflight_identity", None)
    if status == "ready":
        wire.update(candidate_identity="a" * 64, plan_digest="b" * 64)
    wire["delivery_effect"] = {"promotes": promotes}
    if promotes:
        wire["delivery_effect"]["effects"] = [{"stream": "ip", "kind": "auto_apply"}, {"stream": "ip", "kind": "job"}]
    return wire


class CandidateAdapter(ReceiptAdapter):
    """Replace only adapter HTTP and preserve its complete request and response."""

    def __init__(self, wire):
        super().__init__()
        self.wire = wire
        self.bodies = []

    def _handle(self, method, url, **kwargs):
        assert method == "POST"
        assert url == f"{CFG['url']}/api/v1/devices/1788/actions/apply-preflight"
        self.bodies.append(deepcopy(kwargs["json"]))
        return make_response(200, self.wire)


@pytest.mark.parametrize("promotes", (False, True))
def test_candidate_preflight_posts_complete_bodies_and_uses_adapter_effect(promotes):
    candidates = {
        "vlan": {"vlans": [{"vlan_id": 101, "name": "example", "accepted_at": "2026-11-01T00:00:00Z"}]},
        "ip": {"addresses": []},
    }
    wire = candidate_wire(promotes=promotes)
    adapter = CandidateAdapter(wire)
    config, session = adapter.patches()
    with config, session:
        result = adapter_client.apply_candidate_preflight(1788, candidates)
    assert adapter.bodies == [{"candidates": candidates}]
    assert result == wire
    assert apply_preflight.admits_intend(result) is (not promotes)
    if promotes:
        message = apply_preflight.candidate_refusal_message(result)
        assert "ip" in message and "auto_apply" in message and "job" in message


@pytest.mark.parametrize(
    "override",
    (
        {"delivery_effect": None},
        {"delivery_effect": {}},
        {"delivery_effect": {"promotes": 0}},
        {"delivery_effect": {"promotes": False, "effects": []}},
        {"delivery_effect": {"promotes": True}},
        {"delivery_effect": {"promotes": True, "effects": []}},
        {"delivery_effect": {"promotes": True, "effects": [{"stream": "ip", "kind": "job", "id": 1}]}},
        {"delivery_effect": {"promotes": True, "effects": [{"stream": "unknown", "kind": "job"}]}},
        {"delivery_effect": {"promotes": True, "effects": [{"stream": "ip", "kind": None}]}},
        {"preflight_identity": "c" * 64},
        {"candidate_identity": None},
        {"candidate_identity": "A" * 64},
        {"plan_digest": "wrong"},
        {"streams": {"vlan": {"status": "admitted"}}},
        {
            "attempts": [
                {"selection": {"vlan": 41, "ip": 42}, "dropped": ["ip"]},
                {"selection": {"vlan": 41}, "dropped": []},
            ]
        },
        {"error": "invalid"},
        {"refusals": [{}]},
    ),
)
def test_candidate_preflight_rejects_malformed_or_dropped_evidence(override):
    wire = {**candidate_wire(), **override}
    with pytest.raises(adapter_client.AdapterError) as raised:
        apply_preflight.validate_candidate_preflight(wire, {"vlan", "ip"})
    assert raised.value.code == "invalid_response"


@pytest.mark.parametrize("field", ("candidate_identity", "plan_digest", "delivery_effect"))
def test_ready_candidate_requires_both_hashes_and_delivery_effect(field):
    wire = candidate_wire()
    del wire[field]
    with pytest.raises(adapter_client.AdapterError) as raised:
        apply_preflight.validate_candidate_preflight(wire, {"vlan", "ip"})
    assert raised.value.code == "invalid_response"


@pytest.mark.parametrize("status", ("refused", "unavailable", "error"))
def test_non_ready_candidate_admits_nothing_and_carries_no_hash(status):
    wire = candidate_wire(status=status)
    assert apply_preflight.validate_candidate_preflight(wire, {"vlan", "ip"}) == wire
    assert not apply_preflight.admits_intend(wire)
    assert apply_preflight.candidate_refusal_message(wire)
    for key in ("candidate_identity", "plan_digest", "preflight_identity"):
        with pytest.raises(adapter_client.AdapterError) as raised:
            apply_preflight.validate_candidate_preflight({**wire, key: "a" * 64}, {"vlan", "ip"})
        assert raised.value.code == "invalid_response"
    wire.pop("delivery_effect")
    wire["attempts"] = []
    assert apply_preflight.validate_candidate_preflight(wire, {"vlan", "ip"}) == wire


@pytest.mark.parametrize(("key", "section"), (("interface", "interface_config"), ("lacp", "lag")))
def test_candidate_streams_are_delivery_sections_not_delivery_keys(key, section):
    wire = make_apply_preflight({section: 41})
    wire.pop("preflight_identity")
    wire.update(candidate_identity="a" * 64, plan_digest="b" * 64, delivery_effect={"promotes": False})
    assert apply_preflight.validate_candidate_preflight(wire, {section}) == wire
    with pytest.raises(adapter_client.AdapterError) as raised:
        apply_preflight.validate_candidate_preflight(wire, {key})
    assert raised.value.code == "invalid_response"


def test_candidate_cannot_admit_the_survivors_of_a_stored_selection_drop():
    wire = make_apply_preflight(
        {"vlan": 41, "ip": 42}, blocked={"ip": {"status": "blocked", "guard": {"interfaces": [["Ethernet1"]]}}}
    )
    wire.pop("preflight_identity")
    wire.update(candidate_identity="a" * 64, plan_digest="b" * 64, delivery_effect={"promotes": False})
    with pytest.raises(adapter_client.AdapterError) as raised:
        apply_preflight.validate_candidate_preflight(wire, {"vlan", "ip"})
    assert raised.value.code == "invalid_response"


def test_delivery_kinds_are_adapter_answers_and_repeated_effects_are_retained():
    wire = candidate_wire(promotes=True)
    wire["delivery_effect"]["effects"] = [{"stream": "ip", "kind": "networked"}] * 2
    assert apply_preflight.validate_candidate_preflight(wire, {"vlan", "ip"}) == wire
    assert not apply_preflight.admits_intend(wire)
    assert apply_preflight.candidate_refusal_message(wire).count("ip: networked") == 2


def test_candidate_client_rejects_an_invalid_delivery_effect_at_the_http_boundary():
    wire = candidate_wire()
    wire["delivery_effect"] = {"promotes": "false"}
    adapter = CandidateAdapter(wire)
    config, session = adapter.patches()
    with config, session, pytest.raises(adapter_client.AdapterError) as raised:
        adapter_client.apply_candidate_preflight(1788, {"vlan": {"vlans": []}, "ip": {"addresses": []}})
    assert raised.value.code == "invalid_response"
