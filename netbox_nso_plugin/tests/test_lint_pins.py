# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The dev group pins ruff and zizmor; CI and hooks derive them from uv.lock."""

from __future__ import annotations

import re
import shlex
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "lint-format.yaml"
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
SQLPARSE_MINIMUM = Version("0.5.0")


def _has_supported_sqlparse_floor(dependency: str) -> bool:
    try:
        requirement = Requirement(dependency)
    except InvalidRequirement:
        return False
    if requirement.name.casefold() != "sqlparse":
        return False
    for specifier in requirement.specifier:
        if specifier.operator not in {">", ">=", "~=", "==", "==="}:
            continue
        try:
            floor = Version(specifier.version)
        except InvalidVersion:
            continue
        if floor >= SQLPARSE_MINIMUM:
            return True
    return False


def test_sqlparse_is_a_runtime_dependency():
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]

    assert any(_has_supported_sqlparse_floor(dependency) for dependency in dependencies)


@pytest.mark.parametrize("dependency", ["sqlparse>=0.5.0", "sqlparse>0.5.0"])
def test_sqlparse_dependency_accepts_a_supported_floor(dependency):
    assert _has_supported_sqlparse_floor(dependency)


def test_packaging_is_a_direct_test_dependency():
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["dependency-groups"]["dev"]

    assert any(Requirement(dependency).name == "packaging" for dependency in dependencies)


@pytest.mark.parametrize("dependency", ["sqlparse", "sqlparse>=0.4.4"])
def test_sqlparse_dependency_rejects_an_unsupported_floor(dependency):
    assert not _has_supported_sqlparse_floor(dependency)


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
    assert command[:4] == ["uv", "run", "--native-tls", tool], (
        f"the local {hook_id} hook must run {tool} via uv run --native-tls"
    )
    assert hook["language"] == "system"
    return hook


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


def test_ruff_version_has_one_source():
    assert _workflow_tool_commands("ruff") == [["check", "."], ["format", "--check", "."]]
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    assert all(repository["repo"] != "https://github.com/astral-sh/ruff-pre-commit" for repository in config["repos"])
    check_hook = _local_hook("ruff-check", "ruff")
    format_hook = _local_hook("ruff-format", "ruff")
    assert shlex.split(check_hook["entry"])[4:] == [
        "check",
        "--force-exclude",
        "--fix",
        "--exit-non-zero-on-fix",
    ]
    assert shlex.split(format_hook["entry"])[4:] == ["format", "--force-exclude"]
    assert check_hook["types_or"] == format_hook["types_or"] == ["python", "pyi"]
    assert check_hook["require_serial"] is format_hook["require_serial"] is True
    assert _declared_version() == _locked_version()


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


def test_zizmor_version_has_one_source():
    assert _workflow_tool_commands("zizmor") == [[".github/workflows"]]
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    assert all(
        repository["repo"] != "https://github.com/zizmorcore/zizmor-pre-commit" for repository in config["repos"]
    )
    hook = _local_hook("zizmor", "zizmor")
    assert hook["files"] == r"^\.github/workflows/"
    assert hook["pass_filenames"] is True
    assert _declared_zizmor_version() == _locked_zizmor_version()
