# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""CI and hooks execute Ruff and Zizmor from the locked uv environment."""

from __future__ import annotations

import re
import shlex
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "lint-format.yaml"
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"
PYPROJECT = ROOT / "pyproject.toml"


def test_packaging_is_a_direct_test_dependency():
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["dependency-groups"]["dev"]

    assert any(Requirement(dependency).name.casefold() == "packaging" for dependency in dependencies)


def _workflow_tool_commands(tool: str) -> list[list[str]]:
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(rf"(?<![\w-]){tool}==", workflow_text), f"the lint workflow hardcodes a {tool} version"

    workflow = yaml.safe_load(workflow_text)
    commands = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str) and re.search(rf"(?<![\w-]){tool}(?![\w-])", run):
                command = shlex.split(run)
                assert command[:4] == ["uv", "run", "--frozen", tool], (
                    f"the lint workflow must run {tool} via uv run --frozen: {run!r}"
                )
                commands.append(command[4:])

    assert commands, f"the lint workflow does not run {tool} via uv run --frozen"
    return commands


def _local_hook(hook_id: str, tool: str) -> dict[str, object]:
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    hooks = [
        hook
        for repository in config["repos"]
        if repository["repo"] == "local"
        for hook in repository["hooks"]
        if hook["id"] == hook_id
    ]
    assert len(hooks) == 1, f"pre-commit must define one local {hook_id} hook"
    hook = hooks[0]
    command = shlex.split(hook["entry"])
    assert command[:5] == ["uv", "run", "--frozen", "--native-tls", tool], (
        f"the local {hook_id} hook must run {tool} via uv run --frozen --native-tls"
    )
    assert hook["language"] == "system"
    return hook


def test_ruff_consumers_use_the_locked_dependency():
    assert _workflow_tool_commands("ruff") == [["check", "."], ["format", "--check", "."]]
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    assert all(repository["repo"] != "https://github.com/astral-sh/ruff-pre-commit" for repository in config["repos"])
    check_hook = _local_hook("ruff-check", "ruff")
    format_hook = _local_hook("ruff-format", "ruff")
    assert shlex.split(check_hook["entry"])[5:] == [
        "check",
        "--force-exclude",
        "--fix",
        "--exit-non-zero-on-fix",
    ]
    assert shlex.split(format_hook["entry"])[5:] == ["format", "--force-exclude"]
    assert check_hook["types_or"] == format_hook["types_or"] == ["python", "pyi"]
    assert check_hook["require_serial"] is format_hook["require_serial"] is True


def test_ruff_enforces_timezone_aware_datetimes():
    lint = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]

    assert "DTZ" in lint["select"]


def test_workflow_zizmor_must_be_executed_via_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workflow = tmp_path / "lint-format.yaml"
    workflow.write_text(
        """
jobs:
  lint:
    env:
      UNUSED_COMMAND: uv run --frozen zizmor
    steps:
      - run: echo audit
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOW", workflow)

    with pytest.raises(AssertionError, match="does not run zizmor via uv run --frozen"):
        _workflow_tool_commands("zizmor")


def test_zizmor_consumers_use_the_locked_dependency():
    assert _workflow_tool_commands("zizmor") == [[".github/workflows"]]
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    assert all(
        repository["repo"] != "https://github.com/zizmorcore/zizmor-pre-commit" for repository in config["repos"]
    )
    hook = _local_hook("zizmor", "zizmor")
    assert hook["files"] == r"^\.github/workflows/"
    assert hook["pass_filenames"] is True


def test_netbox_checkouts_use_immutable_commits():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "test.yaml").read_text())
    checkouts = 0
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            checkout = step.get("with", {})
            if checkout.get("repository") != "netbox-community/netbox":
                continue
            checkouts += 1
            assert checkout["ref"] == "${{ matrix.environment.netbox-ref }}"
            for environment in job["strategy"]["matrix"]["environment"]:
                assert re.fullmatch(r"[0-9a-f]{40}", environment["netbox-ref"])
    assert checkouts
