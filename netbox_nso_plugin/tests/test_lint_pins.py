# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The commit hook and CI must run the SAME ruff.

An unpinned ruff in CI resolves to whatever is newest at job time, so a new lint or
formatting rule turns CI red on a commit the hook had just passed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "lint-format.yaml"
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"


def _workflow_version() -> str:
    found = re.findall(r"ruff==([0-9][^\s\"']*)", WORKFLOW.read_text())
    assert found, "the lint workflow installs an unpinned ruff"
    assert len(set(found)) == 1, f"the lint workflow installs different ruff versions: {found}"
    return found[0]


def _pre_commit_version() -> str:
    found = re.search(r"repo: https://github\.com/astral-sh/ruff-pre-commit\s*\n\s*rev: v(\S+)", PRE_COMMIT.read_text())
    assert found, "the ruff pre-commit hook has no rev"
    return found.group(1)


def _declared_version() -> str:
    groups = tomllib.loads(PYPROJECT.read_text())["dependency-groups"]["dev"]
    [declared] = [entry for entry in groups if entry.startswith("ruff")]
    found = re.fullmatch(r"ruff==(\S+)", declared)
    assert found, f"the dev group declares an unpinned ruff: {declared!r}"
    return found.group(1)


def _locked_version() -> str:
    for entry in tomllib.loads(UV_LOCK.read_text())["package"]:
        if entry["name"] == "ruff":
            return entry["version"]
    raise AssertionError("ruff is absent from the lock file")


def test_every_ruff_pin_names_one_version():
    versions = {
        "lint-format.yaml": _workflow_version(),
        ".pre-commit-config.yaml": _pre_commit_version(),
        "pyproject.toml": _declared_version(),
        "uv.lock": _locked_version(),
    }

    assert len(set(versions.values())) == 1, f"ruff versions have drifted apart: {versions}"
