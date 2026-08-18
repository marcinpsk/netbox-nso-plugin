# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the local parallel-test database isolation."""

import os
import subprocess
from pathlib import Path

import pytest

from netbox_nso_plugin.tests.conftest import _isolated_test_db_name

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_xdist_worker_gets_a_private_test_database():
    assert _isolated_test_db_name("test_netbox_nso_plugin", "gw3") == "test_netbox_nso_plugin_gw3"


def test_serial_run_keeps_the_unsuffixed_test_database():
    assert _isolated_test_db_name("test_netbox_nso_plugin", None) == "test_netbox_nso_plugin"


def test_real_netbox_ci_disables_root_stubs(monkeypatch):
    # The repo-root conftest is importable only because pytest put the rootdir on
    # sys.path. The Django runner imports this module during discovery — and collects
    # nothing from it, these being plain functions — so a module-level import here would
    # fail the whole run on a module that never contributes a test.
    import conftest as root_conftest

    monkeypatch.setenv("NETBOX_NSO_USE_REAL_NETBOX", "1")

    assert root_conftest._should_inject_netbox_stubs() is False


@pytest.mark.parametrize(("detected_workers", "expected"), [("4", 4), ("32", 8)])
def test_auto_worker_count_never_exceeds_the_cap(monkeypatch, detected_workers, expected):
    # PYTEST_XDIST_AUTO_NUM_WORKERS short-circuits xdist's own CPU detection, so this
    # exercises the real hook chain without depending on the cores of the host.
    import conftest as root_conftest

    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", detected_workers)

    assert root_conftest.MAX_PARALLEL_WORKERS == 8
    assert root_conftest.pytest_xdist_auto_num_workers(None) == expected


@pytest.mark.parametrize(
    ("workers", "expected_args"),
    [(None, "-n 8 --maxschedchunk=1"), ("1", "-n 0")],
)
def test_netbox_test_aliases_request_the_configured_worker_count(workers, expected_args):
    # `-n 0` is what a serial run needs now that the addopts request `-n auto`.
    script = "\n".join(
        (
            f'source "{REPOSITORY_ROOT}/.devcontainer/scripts/load-aliases.sh"',
            "source() { :; }",  # skip the venv activation
            "pytest() { printf 'PYTEST %s\\n' \"$*\"; }",
            "netbox-test",
            "netbox-test-coverage",
        )
    )
    environment = dict(os.environ)
    environment.pop("NETBOX_TEST_WORKERS", None)
    if workers is not None:
        environment["NETBOX_TEST_WORKERS"] = workers

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        cwd=REPOSITORY_ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("PYTEST netbox_nso_plugin/tests ") == 2
    assert result.stdout.count(expected_args) == 2
