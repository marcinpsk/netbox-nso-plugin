# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Regression tests for the release workflow's ref transaction."""

from __future__ import annotations

import subprocess
from pathlib import Path

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "release.yaml"


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


def test_expected_tip_transaction_rejects_branch_advance_before_tag_push(tmp_path: Path):
    remote = tmp_path / "remote.git"
    first = tmp_path / "first"
    second = tmp_path / "second"
    _git(tmp_path, "init", "--bare", str(remote))
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
