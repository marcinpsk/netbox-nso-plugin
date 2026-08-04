# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the local parallel-test database isolation."""

from netbox_nso_plugin.tests.conftest import _isolated_test_db_name


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
