# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the local parallel-test database isolation."""

from netbox_nso_plugin.tests.conftest import _isolated_test_db_name


def test_xdist_worker_gets_a_private_test_database():
    assert _isolated_test_db_name("test_netbox_nso_plugin", "gw3") == "test_netbox_nso_plugin_gw3"


def test_serial_run_keeps_the_unsuffixed_test_database():
    assert _isolated_test_db_name("test_netbox_nso_plugin", None) == "test_netbox_nso_plugin"
