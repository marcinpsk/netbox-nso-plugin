# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_refusal_settlement_fixture_matches_adapter_bytes():
    artifact = os.environ.get("REFUSAL_SETTLEMENT_FIXTURE")
    assert artifact, "REFUSAL_SETTLEMENT_FIXTURE is required by the paired contract gate"
    assert (ROOT / "netbox_nso_plugin/tests/fixtures/refusal_settlement.json").read_bytes() == Path(
        artifact
    ).read_bytes()


def test_refusal_codec_and_corpus_match_pinned_adapter_bytes():
    artifact = os.environ.get("REFUSAL_SETTLEMENT_FIXTURE")
    assert artifact, "REFUSAL_SETTLEMENT_FIXTURE is required by the paired contract gate"
    adapter = Path(artifact).resolve().parents[2] / "nso_adapter/nso"
    for filename in ("refusal_protocol.py", "refusal_protocol_corpus.json", "execution_contract.py"):
        assert (ROOT / "netbox_nso_plugin" / filename).read_bytes() == (adapter / filename).read_bytes()
