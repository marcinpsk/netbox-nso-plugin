# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
import copy
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIGEST = "f5be2d320d5d558b2d076533d37a7a5f0bf52dcffb2b8788c8eb3d54fcfe43f2"
MODULE_DIGEST = "e111f0c5fe73593c5da6a645d308a82210a8966a71408e9c92d9adddd00087cd"
CORPUS_DIGEST = "f7a79288978d9f1d3811fc5fd6621e01a1f1d6346ba74f4495e5dd1134331836"
SOURCE_DIGEST = "fed46e57873717f9e35f173732f2777031f91f6ea23024b4ef3d6fc512574c8f"


def generation():
    return {
        "generation_id": 17,
        "seq": 9,
        "status": "settled",
        "sections": ["bgp"],
        "stream_revisions": {"bgp": 7},
        "source_push_seq": {"bgp": 41},
        "carrier_job_id": 23,
        "carrier_job_status": "succeeded",
        "carrier_job_result": {
            "bgp_count_by_outcome": {"in_sync": 0, "apply_failed": 0, "not_applied": 0, "unavailable": 0},
            "row_outcomes": [],
            "refusals": [],
            "settlement_owners": [],
            "commit_acknowledged": False,
        },
        "carrier_job_error": None,
        "updated_at": "2026-01-02T03:04:05+00:00",
        "row_outcomes": [],
        "refusals": [],
    }


def test_refusal_settlement_fixture_has_pinned_digest():
    assert hashlib.sha256((ROOT / "tests/fixtures/refusal_settlement.json").read_bytes()).hexdigest() == FIXTURE_DIGEST


def test_refusal_protocol_artifacts_match_canonical_bytes():
    from netbox_nso_plugin.refusal_artifacts import verify_artifacts

    module = (ROOT / "refusal_protocol.py").read_bytes()
    corpus = (ROOT / "refusal_protocol_corpus.json").read_bytes()
    assert hashlib.sha256(module).hexdigest() == MODULE_DIGEST
    assert hashlib.sha256(corpus).hexdigest() == CORPUS_DIGEST
    verify_artifacts(module, corpus, source_digest=SOURCE_DIGEST)


@pytest.mark.parametrize("target", ["body", "corpus", "source"])
def test_refusal_protocol_artifact_integrity_detects_byte_change(target):
    from netbox_nso_plugin.refusal_artifacts import ArtifactIntegrityError, verify_artifacts

    module = (ROOT / "refusal_protocol.py").read_bytes()
    corpus = (ROOT / "refusal_protocol_corpus.json").read_bytes()
    if target == "body":
        module += b"\n"
    elif target == "corpus":
        corpus += b"\n"
    else:
        module = module.replace(SOURCE_DIGEST.encode(), b"0" * 64)
    with pytest.raises(ArtifactIntegrityError):
        verify_artifacts(module, corpus, source_digest=SOURCE_DIGEST)


def test_refusal_protocol_corpus_row_identities():
    from netbox_nso_plugin.refusal_protocol import canonical_identity, row_ref

    corpus = json.loads((ROOT / "refusal_protocol_corpus.json").read_bytes())
    for case in corpus["row_refs"]:
        path = tuple((name, tuple(keys)) for name, keys in case["natural_path"])
        assert canonical_identity(case["family"], case["subject"], path) == case["canonical"]
        assert row_ref(case["family"], case["subject"], path).key == case["key"]


def test_refusal_protocol_corpus_messages_parse_and_validate():
    from netbox_nso_plugin.refusal_contract import validate_refusals
    from netbox_nso_plugin.refusal_protocol import RefusalProtocolError, claims_refusal, parse_refusals, render_refusals

    corpus = json.loads((ROOT / "refusal_protocol_corpus.json").read_bytes())
    for case in corpus["valid"]:
        parsed = parse_refusals(case["message"])
        assert render_refusals(parsed) == case["message"], case["name"]
        attributed = [
            {**record, "logical_rows": [], "provenance": "current", "sender_phase": "commit"}
            for record in case["refusals"]
        ]
        assert validate_refusals(attributed) == attributed, case["name"]
    for case in corpus["invalid"]:
        assert claims_refusal(case["message"]) is case["claims"], case["name"]
        with pytest.raises(RefusalProtocolError):
            parse_refusals(case["message"])


def test_cutover_generation_accepts_pinned_adapter_shape():
    from netbox_nso_plugin.refusal_contract import validate_generation

    raw = generation()
    assert validate_generation(raw) == raw


def test_cutover_count_map_accepts_four_keys():
    from netbox_nso_plugin.refusal_contract import validate_count_map

    raw = generation()
    raw["carrier_job_result"]["bgp_count_by_outcome"]["in_sync"] = 1
    assert validate_count_map(raw["carrier_job_result"]["bgp_count_by_outcome"])["in_sync"] == 1


def test_cutover_generation_contract_rejects_old_plugin_shape():
    from netbox_nso_plugin.refusal_contract import ContractError, validate_generation

    raw = generation()
    del raw["row_outcomes"], raw["refusals"]
    with pytest.raises(ContractError, match="shape"):
        validate_generation(raw)


@pytest.mark.parametrize(
    "counts",
    [
        {"in_sync": 0, "apply_failed": 0},
        {"in_sync": 0, "apply_failed": 0, "not_applied": 0, "unavailable": 0, "other": 0},
        {"in_sync": True, "apply_failed": 0, "not_applied": 0, "unavailable": 0},
        {"in_sync": -1, "apply_failed": 0, "not_applied": 0, "unavailable": 0},
    ],
)
def test_cutover_count_map_rejects_invalid_counts(counts):
    from netbox_nso_plugin.refusal_contract import ContractError, validate_count_map

    with pytest.raises(ContractError):
        validate_count_map(counts)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation_id", True),
        ("seq", 0),
        ("updated_at", "2026-01-02T03:04:05"),
        ("status", "unknown"),
        ("row_outcomes", None),
        ("refusals", {}),
        ("carrier_job_id", None),
        ("carrier_job_status", "running"),
    ],
)
def test_cutover_generation_rejects_invalid_terminal_evidence(field, value):
    from netbox_nso_plugin.refusal_contract import ContractError, validate_generation

    raw = generation()
    raw[field] = value
    with pytest.raises(ContractError):
        validate_generation(raw)


def test_cutover_generation_rejects_succeeded_carrier_without_result():
    from netbox_nso_plugin.refusal_contract import ContractError, validate_generation

    raw = generation()
    raw.update(status="failed", carrier_job_result=None, row_outcomes=None, refusals=None)
    with pytest.raises(ContractError, match="succeeded carrier without result"):
        validate_generation(raw)


def test_cutover_generation_preserves_nullable_preterminal_projections():
    from netbox_nso_plugin.refusal_contract import validate_generation

    raw = generation()
    raw.update(
        status="pending",
        carrier_job_id=None,
        carrier_job_status=None,
        carrier_job_result=None,
        row_outcomes=None,
        refusals=None,
    )
    assert validate_generation(raw) == raw


def test_cutover_generation_rejects_projection_drift():
    from netbox_nso_plugin.refusal_contract import ContractError, validate_generation

    raw = copy.deepcopy(generation())
    raw["carrier_job_result"]["refusals"] = [{"message": "placeholder forged cause"}]
    with pytest.raises(ContractError):
        validate_generation(raw)


def test_cutover_generation_accepts_refused_peer_evidence():
    from netbox_nso_plugin.refusal_contract import validate_generation

    from .test_refusal_result import refused_bgp_result

    result = refused_bgp_result()
    raw = generation()
    raw.update(
        status="failed",
        carrier_job_status="failed",
        row_outcomes=result["row_outcomes"],
        refusals=result["refusals"],
        carrier_job_result=result,
    )
    assert validate_generation(raw) == raw


@pytest.mark.parametrize("section", ["bgp", "interface_config", "isis"])
def test_cutover_generation_requires_promised_count_maps(section):
    from netbox_nso_plugin.refusal_contract import ContractError, validate_generation

    raw = generation()
    raw["sections"] = [section]
    raw["carrier_job_result"] = {
        "row_outcomes": [],
        "refusals": [],
        "settlement_owners": [],
        "commit_acknowledged": False,
    }
    with pytest.raises(ContractError, match="count"):
        validate_generation(raw)


@pytest.mark.parametrize(
    ("stream", "section", "counters"),
    [
        ("ip", "interface_config", ("attribute_count_by_outcome", "ip_count_by_outcome")),
        ("isis_flex_algo", "isis", ("isis_count_by_outcome",)),
    ],
)
def test_split_section_streams_count_under_their_document_section(stream, section, counters):
    """The adapter reports generation sections through stream_section(), never as stream names."""
    from netbox_nso_plugin.refusal_contract import ContractError, validate_generation

    raw = generation()
    counts = raw["carrier_job_result"]["bgp_count_by_outcome"]
    raw.update(sections=[section], stream_revisions={stream: 7}, source_push_seq={stream: 41})
    raw["carrier_job_result"] = {
        **{key: dict(counts) for key in counters},
        "row_outcomes": [],
        "refusals": [],
        "settlement_owners": [],
        "commit_acknowledged": False,
    }
    assert validate_generation(raw) == raw
    raw["sections"] = [stream]
    with pytest.raises(ContractError, match="invalid counted section"):
        validate_generation(raw)


def test_cutover_generation_rejects_unknown_carrier_status():
    from netbox_nso_plugin.refusal_contract import ContractError, validate_generation

    raw = generation()
    raw.update(status="failed", carrier_job_status="cancelled")
    with pytest.raises(ContractError, match="carrier status"):
        validate_generation(raw)
