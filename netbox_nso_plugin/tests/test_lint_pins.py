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

import pytest
import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "lint-format.yaml"
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"


def _workflow_version() -> str:
    found = re.findall(r"ruff==([0-9][^\s\"']*)", WORKFLOW.read_text(encoding="utf-8"))
    assert found, "the lint workflow installs an unpinned ruff"
    assert len(set(found)) == 1, f"the lint workflow installs different ruff versions: {found}"
    return found[0]


def _pre_commit_version() -> str:
    found = re.search(
        r"repo: https://github\.com/astral-sh/ruff-pre-commit\s*\n\s*rev: v(\S+)",
        PRE_COMMIT.read_text(encoding="utf-8"),
    )
    assert found, "the ruff pre-commit hook has no rev"
    return found.group(1)


def _declared_version() -> str:
    groups = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["dependency-groups"]["dev"]
    [declared] = [entry for entry in groups if entry.startswith("ruff")]
    found = re.fullmatch(r"ruff==(\S+)", declared)
    assert found, f"the dev group declares an unpinned ruff: {declared!r}"
    return found.group(1)


def _locked_version() -> str:
    for entry in tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]:
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


def _workflow_zizmor_version() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    references = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            executed_values = [step.get("run"), step.get("uses"), *step.get("with", {}).values()]
            references.extend(
                match
                for value in executed_values
                if isinstance(value, str)
                for match in re.finditer(r"(?<![\w-])zizmor(?:==([0-9][^\s\"']*))?(?![\w-])", value)
            )

    assert references, "the lint workflow does not install or run zizmor"
    unpinned = [match.string for match in references if match.group(1) is None]
    assert not unpinned, f"the lint workflow has an unpinned zizmor reference: {unpinned}"
    versions = [match.group(1) for match in references]
    assert len(set(versions)) == 1, f"the lint workflow installs different zizmor versions: {versions}"
    return versions[0]


def test_workflow_zizmor_pin_must_be_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow = tmp_path / "lint-format.yaml"
    workflow.write_text(
        """
jobs:
  lint:
    env:
      UNUSED_PIN: zizmor==1.29.0
    steps:
      - run: uv pip install zizmor
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("netbox_nso_plugin.tests.test_lint_pins.WORKFLOW", workflow)

    with pytest.raises(AssertionError, match="unpinned zizmor"):
        _workflow_zizmor_version()


def _pre_commit_zizmor_version() -> str:
    found = re.search(
        r"repo: https://github\.com/zizmorcore/zizmor-pre-commit\s*\n\s*rev: v(\S+)",
        PRE_COMMIT.read_text(encoding="utf-8"),
    )
    assert found, "the zizmor pre-commit hook has no rev"
    return found.group(1)


def _declared_zizmor_version() -> str:
    groups = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["dependency-groups"]["dev"]
    [declared] = [entry for entry in groups if entry.startswith("zizmor")]
    found = re.fullmatch(r"zizmor==(\S+)", declared)
    assert found, f"the dev group declares an unpinned zizmor: {declared!r}"
    return found.group(1)


def _locked_zizmor_version() -> str:
    for entry in tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]:
        if entry["name"] == "zizmor":
            return entry["version"]
    raise AssertionError("zizmor is absent from the lock file")


def test_every_zizmor_pin_names_one_version():
    versions = {
        "lint-format.yaml": _workflow_zizmor_version(),
        ".pre-commit-config.yaml": _pre_commit_zizmor_version(),
        "pyproject.toml": _declared_zizmor_version(),
        "uv.lock": _locked_zizmor_version(),
    }

    assert len(set(versions.values())) == 1, f"zizmor versions have drifted apart: {versions}"
