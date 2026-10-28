# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
import copy
import hashlib
import json
from pathlib import Path

import pytest

from netbox_nso_plugin import execution_contract, refusal_contract
from netbox_nso_plugin.delivery import delivery_keys
from netbox_nso_plugin.refusal_protocol import SUBJECTS
from netbox_nso_plugin.refusal_registry import OVERLAYS, SECTION_COUNTER_KEYS, owner_spec

from . import execution_contract_oracle as oracle
from .test_refusal_contract import generation
from .test_refusal_result import refused_bgp_result

_FIXTURES = Path(__file__).parent / "fixtures"


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


def test_oracle_is_the_frozen_pre_extraction_validator():
    source = (
        Path(oracle.__file__)
        .read_text(encoding="utf-8")
        .replace("from .. import refusal_protocol", "from . import refusal_protocol")
    )
    source = source.replace("from ..delivery", "from .delivery").replace(
        "from ..refusal_registry", "from .refusal_registry"
    )
    assert (
        hashlib.sha256(source.encode()).hexdigest()
        == "01943c2be45d9682110d045413f7f69f2548c8f31cd6a54b36943dc8d975ed40"
    )
    assert "execution_contract" not in source


def _verdict(module, validator, raw, **kwargs):
    value = copy.deepcopy(raw)
    try:
        validated = getattr(module, validator)(value, **kwargs)
    except module.ContractError as exc:
        return "reject", str(exc)
    assert validated is value
    assert value == raw
    return "accept", None


def _results():
    results = {"settlement": refused_bgp_result()}
    for path in sorted(_FIXTURES.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "row_outcomes" in raw:
            results[path.stem] = raw
    fixture = json.loads((_FIXTURES / "refusal_settlement.json").read_text(encoding="utf-8"))
    for case in fixture["mapping_cases"]:
        raw = copy.deepcopy(results["settlement"])
        row = copy.deepcopy(raw["row_outcomes"][0])
        stream = {"route-policy": "route_policy"}.get(case["family"], case["family"])
        row.update(stream=stream, row=case["logical"])
        row["carrier"].update(stream=stream, row=case["carrier"])
        catalog = {**fixture["bgp"]["refusal"], "family": case["family"], "row": case["cause"]}
        row["refusals"] = [catalog]
        raw = {
            f"{stream}_count_by_outcome": {"in_sync": 0, "apply_failed": 1, "not_applied": 0, "unavailable": 0},
            "row_outcomes": [row],
            "refusals": [
                {
                    **catalog,
                    "logical_rows": [
                        {
                            key: copy.deepcopy(value)
                            for key, value in row.items()
                            if key not in {"disposition", "refusals"}
                        }
                    ],
                    "provenance": "current",
                    "sender_phase": "commit",
                }
            ],
            "settlement_owners": [{**copy.deepcopy(row["carrier"]), "disposition": "refused"}],
            "commit_acknowledged": False,
        }
        results[case["cause"]["subject"]] = raw
    return results


_MUTATIONS = (
    "unchanged",
    "drop_outcome",
    "duplicate_outcome",
    "drop_cause",
    "duplicate_cause",
    "drop_logical_cause",
    "foreign_current_cause",
    "foreign_retained_cause",
    "foreign_prior_cause",
    "count",
    "missing_count",
    "missing_carrier",
    "duplicate_carrier",
    "carrier_disposition",
    "carrier_stream",
    "carrier_subject",
    "post_commit_without_acknowledgement",
    "acknowledged",
    "cohort",
    "generation",
    "revision",
    "push",
    "acceptance",
    "invalid_provenance",
    "invalid_subject",
    "invalid_digest",
    "catalog_message",
)


def _mutate(raw, mutation):
    outcome = raw["row_outcomes"][0]
    cause = raw["refusals"][0]
    logical = cause["logical_rows"][0]
    identity_fields = {
        "cohort": "settlement_cohort",
        "generation": "generation_id",
        "revision": "frozen_revision",
        "push": "source_push_seq",
        "acceptance": "acceptance_identity",
    }
    if mutation in identity_fields:
        field = identity_fields[mutation]
        logical[field] = "2026-01-03T03:04:05+00:00" if mutation == "acceptance" else 99
    elif mutation.startswith("foreign_"):
        foreign = copy.deepcopy(logical)
        foreign["row"]["key"] = "f" * 64
        cause["logical_rows"].append(foreign)
        cause["provenance"] = mutation.split("_")[1]
    elif mutation == "drop_outcome":
        raw["row_outcomes"].pop(0)
    elif mutation == "duplicate_outcome":
        raw["row_outcomes"].append(copy.deepcopy(outcome))
    elif mutation == "drop_cause":
        raw["refusals"] = []
    elif mutation == "duplicate_cause":
        raw["refusals"].append(copy.deepcopy(cause))
    elif mutation == "drop_logical_cause":
        outcome["refusals"] = []
    elif mutation == "count":
        next(value for key, value in raw.items() if key.endswith("_count_by_outcome"))["apply_failed"] += 1
    elif mutation == "missing_count":
        del raw[next(key for key in raw if key.endswith("_count_by_outcome") and raw[key]["apply_failed"])]
    elif "carrier" in mutation:
        _mutate_carrier(raw, mutation)
    else:
        _mutate_cause(raw, mutation)


def _mutate_cause(raw, mutation):
    cause = raw["refusals"][0]
    logical = cause["logical_rows"][0]
    if mutation == "post_commit_without_acknowledgement":
        cause["sender_phase"] = "post_commit_verification"
    elif mutation == "acknowledged":
        raw["commit_acknowledged"] = True
    elif mutation == "invalid_provenance":
        cause["provenance"] = "unknown"
    elif mutation == "invalid_subject":
        logical["row"]["subject"] = "unknown"
    elif mutation == "invalid_digest":
        logical["row"]["key"] = "invalid"
    elif mutation == "catalog_message":
        cause["message"] = "placeholder altered message"


def _mutate_carrier(raw, mutation):
    if mutation == "missing_carrier":
        raw["settlement_owners"] = []
    elif mutation == "duplicate_carrier":
        raw["settlement_owners"].append(copy.deepcopy(raw["settlement_owners"][0]))
    elif mutation.startswith("carrier_"):
        carrier = raw["settlement_owners"][0]
        if mutation == "carrier_disposition":
            carrier["disposition"] = "in_sync"
        elif mutation == "carrier_stream":
            carrier["stream"] = "unknown"
        else:
            carrier["row"]["subject"] = "bgp.router.scope.peer.peer-address-family"


@pytest.mark.parametrize("name,result", _results().items())
@pytest.mark.parametrize("mutation", _MUTATIONS)
def test_result_verdict_matches_frozen_oracle(name, result, mutation):
    raw = copy.deepcopy(result)
    _mutate(raw, mutation)
    expected = _verdict(oracle, "validate_result", raw)
    assert _verdict(refusal_contract, "validate_result", raw) == expected, (name, mutation)
    assert _verdict(refusal_contract, "validate_refusals", raw["refusals"]) == _verdict(
        oracle, "validate_refusals", raw["refusals"]
    )


def test_differential_corpus_contains_accepted_results_and_foreign_rejection():
    for name, raw in _results().items():
        assert _verdict(oracle, "validate_result", raw) == ("accept", None), name
        _mutate(raw, "foreign_current_cause")
        assert _verdict(oracle, "validate_result", raw) == ("reject", "refusal names an unattempted logical row")


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation_id", 99),
        ("seq", 0),
        ("status", "unknown"),
        ("updated_at", "2026-01-02"),
        ("sections", ["unknown"]),
        ("stream_revisions", {"bgp": 99}),
        ("source_push_seq", {"bgp": 99}),
        ("carrier_job_id", None),
        ("carrier_job_status", "running"),
        ("row_outcomes", None),
        ("refusals", []),
        ("carrier_job_result", None),
        ("unchanged", None),
    ],
)
def test_generation_verdict_matches_frozen_oracle(field, value):
    raw = generation()
    result = refused_bgp_result()
    raw.update(
        status="failed",
        carrier_job_status="failed",
        carrier_job_result=result,
        row_outcomes=copy.deepcopy(result["row_outcomes"]),
        refusals=copy.deepcopy(result["refusals"]),
    )
    if field != "unchanged":
        raw[field] = value
    for validator, argument in (("validate_generation", raw), ("validate_carrier_generations", [raw])):
        assert _verdict(refusal_contract, validator, argument) == _verdict(oracle, validator, argument)


@pytest.mark.parametrize(
    "counts",
    [
        {"in_sync": 0, "apply_failed": 1, "not_applied": 0, "unavailable": 0},
        {"in_sync": True, "apply_failed": 0, "not_applied": 0, "unavailable": 0},
        {"in_sync": -1, "apply_failed": 0, "not_applied": 0, "unavailable": 0},
        {},
    ],
)
def test_count_verdict_matches_frozen_oracle(counts):
    assert _verdict(refusal_contract, "validate_count_map", counts) == _verdict(oracle, "validate_count_map", counts)


def test_existing_contract_consumers_keep_their_shared_rules():
    from netbox_nso_plugin.intent_receipt_contract import validate_intent_receipts
    from netbox_nso_plugin.refusal_display import catalog_causes

    receipt = {"receipts": [], "global_max_push_seq": None, "global_max_route_id": None}
    assert validate_intent_receipts(receipt) is receipt
    cause = refused_bgp_result()["row_outcomes"][0]["refusals"][0]
    assert catalog_causes([cause]) == [cause]
