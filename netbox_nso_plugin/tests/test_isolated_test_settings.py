# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The isolated settings module refuses shared state and live adapter configuration."""

import os
import runpy
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import netbox.settings as netbox_settings
from django.test import SimpleTestCase

SETTINGS = Path(__file__).resolve().parents[2] / "isolated_test_settings.py"


class TestIsolatedTestSettings(SimpleTestCase):
    def test_missing_database_name_reports_the_required_command_shape(self):
        with patch.dict(os.environ, {}):
            os.environ.pop("TEST_DB_NAME", None)
            with self.assertRaisesRegex(RuntimeError, r"TEST_DB_NAME.*TEST_DB_NAME=test_nso_<tag>"):
                runpy.run_path(SETTINGS)

    def test_unsafe_database_names_are_rejected_before_django_can_create_or_drop_them(self):
        for name in ("netbox", "test_netbox_nso_plugin", "test_nso_"):
            with self.subTest(name=name):
                with patch.dict(os.environ, {"TEST_DB_NAME": name}):
                    with self.assertRaisesRegex(RuntimeError, r"private.*test_nso_<tag>"):
                        runpy.run_path(SETTINGS)

    def test_runner_settings_replace_the_database_and_adapter_configuration(self):
        with patch.dict(os.environ, {"TEST_DB_NAME": "test_nso_settings"}):
            configured = runpy.run_path(SETTINGS)

        self.assertEqual(configured["DATABASES"]["default"]["TEST"]["NAME"], "test_nso_settings")
        adapter = configured["PLUGINS_CONFIG"]["netbox_nso_plugin"]
        self.assertEqual(adapter["adapter_url"], "http://adapter.mock.invalid")
        self.assertEqual(adapter["adapter_token"], "test-token")

    def test_runner_settings_do_not_mutate_the_cached_netbox_database_configuration(self):
        original = deepcopy(netbox_settings.DATABASES)
        try:
            with patch.dict(os.environ, {"TEST_DB_NAME": "test_nso_settings"}):
                runpy.run_path(SETTINGS)

            self.assertEqual(netbox_settings.DATABASES, original)
        finally:
            netbox_settings.DATABASES.clear()
            netbox_settings.DATABASES.update(original)
