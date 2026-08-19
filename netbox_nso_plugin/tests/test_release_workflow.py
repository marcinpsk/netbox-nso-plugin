# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Regression tests for the release workflow's ref transaction."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "release.yaml"
PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"
UV_LOCK = Path(__file__).parents[2] / "uv.lock"


def _locked_version(lock_text: str, package: str) -> str:
    """Return the version ``uv.lock`` records for one package."""
    for entry in tomllib.loads(lock_text)["package"]:
        if entry["name"] == package:
            return entry["version"]
    raise AssertionError(f"{package} is absent from the lock file")


def _uv_lock(cwd: Path) -> None:
    """Refresh the lock the way the release build does, failing by name when uv is absent.

    A missing tool is a broken environment for a release pin, not a reason to pass: this
    asserts rather than skipping, so the gap is visible instead of silently green.
    """
    assert shutil.which("uv"), "uv is not on PATH, so the release lock step cannot be verified"
    result = subprocess.run(["uv", "lock", "--offline"], cwd=cwd, check=False, text=True, capture_output=True)
    assert result.returncode == 0, f"uv lock --offline failed: {result.stderr}"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, contents: str, message: str) -> str:
    (repo / "release.txt").write_text(contents)
    _git(repo, "add", "release.txt")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_release_refs_use_one_expected_tip_transaction():
    workflow = WORKFLOW.read_text()
    release_action_start = workflow.index("- name: Release from conventional commits")
    publish_start = workflow.index("- name: Publish release refs with expected-tip lease")
    release_action = workflow[release_action_start:publish_start]
    release_start = workflow.index("- name: Create the GitHub release", publish_start)
    publish_step = workflow[publish_start:release_start]

    assert "git push --atomic" in publish_step
    assert '--force-with-lease="refs/heads/${RELEASE_REF}:${RELEASE_SHA}"' in publish_step
    assert publish_step.index('git fetch --quiet origin "refs/heads/${RELEASE_REF}"') < publish_step.index(
        "git push --atomic"
    )
    assert 'git push origin "v${version}"' not in workflow
    assert "push: false" in release_action
    assert "vcs_release: false" in release_action


def test_the_release_commit_carries_a_regenerated_lock():
    """The lock records this project's own version, so the release commit has to refresh it."""
    config = tomllib.loads(PYPROJECT.read_text())["tool"]["semantic_release"]
    assert config["build_command"] == "uv lock"
    assert config["assets"] == ["uv.lock"]

    workflow = WORKFLOW.read_text()
    release_action_start = workflow.index("- name: Release from conventional commits")
    publish_start = workflow.index("- name: Publish release refs with expected-tip lease")
    release_action = workflow[release_action_start:publish_start]

    # The action maps build: false to --skip-build, which silently drops build_command.
    assert "build: true" in release_action
    assert "astral-sh/setup-uv" in workflow[:release_action_start]


def test_uv_lock_records_the_declared_project_version():
    """The committed lock must not drift from the version a release would cut."""
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert _locked_version(UV_LOCK.read_text(), "netbox-nso-plugin") == declared


def test_uv_lock_follows_a_version_bump(tmp_path: Path):
    """The bump alone leaves the lock stale; ``uv lock`` is what re-pins it."""
    # No dependencies, so the resolve needs no network.
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "demo-pkg"\nversion = "0.2.0"\n'
        'requires-python = ">=3.9"\ndependencies = []\n\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    )
    _uv_lock(tmp_path)
    assert _locked_version((tmp_path / "uv.lock").read_text(), "demo-pkg") == "0.2.0"

    project.write_text(project.read_text().replace('version = "0.2.0"', 'version = "0.3.0"'))
    assert _locked_version((tmp_path / "uv.lock").read_text(), "demo-pkg") == "0.2.0", (
        "the version bump alone must not touch the lock"
    )

    _uv_lock(tmp_path)
    assert _locked_version((tmp_path / "uv.lock").read_text(), "demo-pkg") == "0.3.0"


def test_expected_tip_transaction_rejects_branch_advance_before_tag_push(tmp_path: Path):
    remote = tmp_path / "remote.git"
    first = tmp_path / "first"
    second = tmp_path / "second"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    _git(tmp_path, "init", "--initial-branch=main", str(first))
    _git(first, "config", "user.name", "release-test")
    _git(first, "config", "user.email", "release-test@example.invalid")
    release_sha = _commit(first, "release", "release source")
    _git(first, "remote", "add", "origin", str(remote))
    _git(first, "push", "--set-upstream", "origin", "main")
    _git(tmp_path, "clone", str(remote), str(second))
    _git(second, "config", "user.name", "release-test")
    _git(second, "config", "user.email", "release-test@example.invalid")

    _commit(second, "concurrent", "concurrent source")
    _git(second, "push", "origin", "main")
    _git(first, "tag", "v0.1.0", release_sha)
    result = subprocess.run(
        [
            "git",
            "push",
            "--atomic",
            f"--force-with-lease=refs/heads/main:{release_sha}",
            "origin",
            f"{release_sha}:refs/heads/main",
            "v0.1.0",
        ],
        cwd=first,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert _git(remote, "show-ref", "--verify", "--hash", "refs/heads/main") != release_sha
    assert (
        subprocess.run(
            ["git", "--git-dir", str(remote), "show-ref", "--verify", "refs/tags/v0.1.0"],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )
