# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Regenerate observation schemas from the live-adapter job's sibling checkout."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NSO_LIVE_ADAPTER_TEST") != "1" and not os.environ.get("NSO_ADAPTER_OPENAPI_SNAPSHOT"),
    reason="requires the dedicated live-adapter job or an explicit NSO_ADAPTER_OPENAPI_SNAPSHOT",
)


def test_vendored_observation_schemas_match_adapter_snapshot(tmp_path):
    root = Path(__file__).resolve().parents[1]
    sibling = Path(__file__).resolve().parents[2] / "nso-adapter/tests/api/openapi_snapshot.json"
    snapshot = Path(os.environ.get("NSO_ADAPTER_OPENAPI_SNAPSHOT", sibling))
    output = tmp_path / "observations.json"
    subprocess.run(
        [sys.executable, str(root / "scripts/generate_observation_schemas.py"), str(snapshot), "--output", str(output)],
        check=True,
    )
    assert output.read_text(encoding="utf-8") == (root / "netbox_nso_plugin/schemas/observations.json").read_text(
        encoding="utf-8"
    )
