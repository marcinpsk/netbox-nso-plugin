# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Validate complete execution evidence before a consumer can use its counts."""

import copy
import json
from pathlib import Path

import pytest

from netbox_nso_plugin import refusal_contract


def refused_bgp_result():
    scenario = json.loads((Path(__file__).parent / "fixtures/refusal_settlement.json").read_text(encoding="utf-8"))[
        "bgp"
    ]
    outcomes = []
    for item in scenario["expected"]:
        outcome = {key: copy.deepcopy(value) for key, value in item.items() if key not in {"outcome", "cause"}}
        outcome["disposition"] = item["outcome"]
        outcome["refusals"] = [copy.deepcopy(scenario["refusal"])] if item["cause"] else []
        outcomes.append(outcome)
    carrier = outcomes[0]["carrier"]
    router = {
        key: copy.deepcopy(value) for key, value in outcomes[0].items() if key not in {"row", "disposition", "refusals"}
    }
    router.update(row=copy.deepcopy(carrier["row"]), disposition="not_applied", refusals=[])
    outcomes.append(router)
    cause = {
        **copy.deepcopy(scenario["refusal"]),
        "logical_rows": [
            {key: copy.deepcopy(value) for key, value in outcomes[0].items() if key not in {"disposition", "refusals"}}
        ],
        "provenance": "current",
        "sender_phase": "commit",
    }
    return {
        "bgp_count_by_outcome": {"in_sync": 0, "apply_failed": 1, "not_applied": 0, "unavailable": 0},
        "row_outcomes": outcomes,
        "refusals": [cause],
        "settlement_owners": [{**copy.deepcopy(carrier), "disposition": "refused"}],
        "commit_acknowledged": False,
    }


def test_child_refusal_counts_one_carrier_and_retains_all_three_owners():
    result = refused_bgp_result()
    assert refusal_contract.validate_result(result, sections=("bgp",)) is result
    assert len(result["row_outcomes"]) == 3


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result["row_outcomes"].append(copy.deepcopy(result["row_outcomes"][0])),
        lambda result: result["settlement_owners"].append(copy.deepcopy(result["settlement_owners"][0])),
        lambda result: result["settlement_owners"][0].update(disposition="not_applied"),
        lambda result: result["bgp_count_by_outcome"].update(apply_failed=2),
        lambda result: result["row_outcomes"][0]["carrier"].update(source_push_seq=42),
        lambda result: result["refusals"][0]["logical_rows"][0].update(frozen_revision=8),
        lambda result: result.update(refusals=[]),
        lambda result: result["row_outcomes"][0].update(refusals=[]),
        lambda result: result["row_outcomes"][0].update(disposition="in_sync"),
        lambda result: result.update(commit_acknowledged=True),
    ],
)
def test_execution_rejects_inconsistent_complete_evidence(mutation):
    result = refused_bgp_result()
    mutation(result)
    with pytest.raises(refusal_contract.ContractError):
        refusal_contract.validate_result(result, sections=("bgp",))


def test_catalog_message_cannot_carry_untrusted_response_text():
    result = refused_bgp_result()
    result["refusals"][0]["message"] = "placeholder-secret-canary"
    with pytest.raises(refusal_contract.ContractError) as caught:
        refusal_contract.validate_result(result, sections=("bgp",))
    assert "placeholder-secret-canary" not in str(caught.value)


def test_job_evidence_rejects_an_unknown_stream_even_with_matching_counts():
    result = refused_bgp_result()
    for row in result["row_outcomes"] + result["refusals"][0]["logical_rows"]:
        row["stream"] = "unknown"
        row["carrier"]["stream"] = "unknown"
    result["settlement_owners"][0]["stream"] = "unknown"
    with pytest.raises(refusal_contract.ContractError):
        refusal_contract.validate_result(result)


def test_post_commit_refusal_cannot_claim_the_transaction_was_rolled_back():
    result = refused_bgp_result()
    result["refusals"][0]["sender_phase"] = "post_commit_verification"
    with pytest.raises(refusal_contract.ContractError):
        refusal_contract.validate_result(result)
