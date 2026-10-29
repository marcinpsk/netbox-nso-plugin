# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba

import copy

import pytest

from netbox_nso_plugin import execution_contract, refusal_contract, refusal_protocol
from netbox_nso_plugin.delivery import delivery_keys
from netbox_nso_plugin.refusal_protocol import SUBJECTS
from netbox_nso_plugin.refusal_registry import OVERLAYS, SECTION_COUNTER_KEYS, owner_spec

from .test_refusal_result import refused_bgp_result


def test_registry_projections_match_execution_metadata():
    metadata = execution_contract.METADATA
    assert metadata["subject_counter_keys"] == {
        subject: list(owner_spec(subject).counter_keys) for spec in OVERLAYS.values() for subject in spec.subjects
    }
    assert metadata["stream_families"] == {
        entry.section: sorted(
            {
                SUBJECTS[subject].family
                for spec in OVERLAYS.values()
                if spec.delivery_keys == (scope,)
                for subject in spec.subjects
            }
        )
        for scope, entry in delivery_keys().items()
    }
    assert metadata["section_counter_keys"] == {key: list(value) for key, value in SECTION_COUNTER_KEYS.items()}
    for model, subject, carrier in metadata["overlay_grain"].values():
        assert owner_spec(subject).model_label == f"netbox_nso_plugin.{model.lower()}"
        assert carrier in metadata["subject_counter_keys"]


def test_public_validators_use_the_canonical_rules():
    for name in (
        "validate_result",
        "validate_refusals",
        "validate_count_map",
        "validate_generation",
        "validate_carrier_generations",
        "ContractError",
    ):
        assert getattr(refusal_contract, name) is getattr(execution_contract, name)


def test_existing_contract_consumers_keep_their_shared_rules():
    from netbox_nso_plugin.intent_receipt_contract import validate_intent_receipts
    from netbox_nso_plugin.refusal_display import catalog_causes

    receipt = {"receipts": [], "global_max_push_seq": None, "global_max_route_id": None}
    assert validate_intent_receipts(receipt) is receipt
    cause = refused_bgp_result()["row_outcomes"][0]["refusals"][0]
    assert catalog_causes([cause]) == [cause]


def test_logical_key_matches_the_attempted_row_and_not_a_foreign_row():
    result = refused_bgp_result()
    attempted = {refusal_contract.logical_key(row) for row in result["row_outcomes"]}
    logical = result["refusals"][0]["logical_rows"][0]
    foreign = copy.deepcopy(logical)
    foreign["row"]["key"] = "f" * 64
    assert refusal_contract.logical_key(logical) in attempted
    assert refusal_contract.logical_key(foreign) not in attempted


def _unapplied(result):
    for row in result["row_outcomes"]:
        row.update(disposition="not_applied", refusals=[])
    result["settlement_owners"][0]["disposition"] = "not_applied"
    result["bgp_count_by_outcome"].update(apply_failed=0, not_applied=1)
    return result


def test_current_document_cause_may_name_a_foreign_natural_row():
    result = _unapplied(refused_bgp_result())
    result["refusals"][0]["logical_rows"][0]["row"]["key"] = "f" * 64
    assert execution_contract.validate_result(result) is result


@pytest.mark.parametrize(
    "field",
    ["settlement_cohort", "generation_id", "frozen_revision", "source_push_seq", "acceptance_identity", "carrier"],
)
def test_current_natural_row_collision_rejects_changed_fences(field):
    result = _unapplied(refused_bgp_result())
    logical = result["refusals"][0]["logical_rows"][0]
    if field == "acceptance_identity":
        logical[field] = "2026-01-03T03:04:05+00:00"
    elif field == "carrier":
        logical[field]["generation_id"] += 1
    else:
        logical[field] += 1
    with pytest.raises(execution_contract.ContractError):
        execution_contract.validate_result(result)


@pytest.mark.parametrize("provenance", ["retained", "prior"])
@pytest.mark.parametrize(
    "field",
    ["settlement_cohort", "generation_id", "frozen_revision", "source_push_seq", "acceptance_identity", "carrier"],
)
def test_historical_natural_row_collision_is_exempt(provenance, field):
    result = _unapplied(refused_bgp_result())
    cause = result["refusals"][0]
    cause["provenance"] = provenance
    logical = cause["logical_rows"][0]
    if field == "acceptance_identity":
        logical[field] = "2026-01-03T03:04:05+00:00"
    elif field == "carrier":
        logical[field]["generation_id"] += 1
    else:
        logical[field] += 1
    assert execution_contract.validate_result(result) is result


@pytest.mark.parametrize("phase", ["pre_commit_dry_run", "commit"])
@pytest.mark.parametrize("provenance", ["current", "retained", "prior"])
def test_unacknowledged_refusal_requires_all_matching_causes(phase, provenance):
    result = _unapplied(refused_bgp_result())
    result["refusals"][0].update(sender_phase=phase, provenance=provenance)
    with pytest.raises(execution_contract.ContractError, match="incomplete logical refusal"):
        execution_contract.validate_result(result)


@pytest.mark.parametrize("phase", ["pre_commit_dry_run", "commit"])
def test_unacknowledged_refusal_accepts_exact_cause_set(phase):
    result = refused_bgp_result()
    result["refusals"][0]["sender_phase"] = phase
    assert execution_contract.validate_result(result) is result


@pytest.mark.parametrize("phase", ["pre_commit_dry_run", "commit", "post_commit_verification"])
def test_acknowledged_execution_keeps_subset_rule(phase):
    result = _unapplied(refused_bgp_result())
    result["commit_acknowledged"] = True
    result["refusals"][0]["sender_phase"] = phase
    assert execution_contract.validate_result(result) is result


def test_execution_without_attributed_causes_keeps_subset_rule():
    result = _unapplied(refused_bgp_result())
    result["refusals"] = []
    assert execution_contract.validate_result(result) is result


def test_subset_rule_still_rejects_unattributed_outcome_cause():
    result = refused_bgp_result()
    result["refusals"] = []
    with pytest.raises(execution_contract.ContractError, match="lacks attributed evidence"):
        execution_contract.validate_result(result)


def test_current_cause_can_link_attempted_and_foreign_rows_together():
    result = refused_bgp_result()
    foreign = copy.deepcopy(result["refusals"][0]["logical_rows"][0])
    foreign["row"]["key"] = "f" * 64
    result["refusals"][0]["logical_rows"].append(foreign)
    assert execution_contract.validate_result(result) is result


def test_unacknowledged_refusal_rejects_a_proper_subset_of_matching_causes():
    result = refused_bgp_result()
    extra = copy.deepcopy(result["refusals"][0])
    message = refusal_protocol.MESSAGES["parent-absent"]
    extra.update(message_id=message.id, reason=message.reason, message=message.text)
    result["refusals"].append(extra)
    with pytest.raises(execution_contract.ContractError, match="incomplete logical refusal"):
        execution_contract.validate_result(result)
