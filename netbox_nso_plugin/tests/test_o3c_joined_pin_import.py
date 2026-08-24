# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Collection boundary tests for the optional O3c joined pin suite."""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase


class TestO3CJoinedPinImport(SimpleTestCase):
    def test_a_missing_workflow_fails_the_o3c_case_instead_of_collection(self):
        module_name = "netbox_nso_plugin.tests.test_o3c_joined_pin"
        loaded = sys.modules.pop(module_name, None)
        try:
            with patch.object(Path, "read_text", side_effect=FileNotFoundError("workflow missing")):
                module = importlib.import_module(module_name)
                with self.assertRaisesRegex(FileNotFoundError, "workflow missing"):
                    module._adapter_commit_from_workflow()
        finally:
            sys.modules.pop(module_name, None)
            if loaded is not None:
                sys.modules[module_name] = loaded
