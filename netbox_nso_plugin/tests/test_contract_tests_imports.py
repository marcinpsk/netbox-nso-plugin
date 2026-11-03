# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Keep the CI-only contract tests importable from the normal suite."""

import ast
import importlib
from pathlib import Path

_CONTRACT_TESTS = Path(__file__).resolve().parents[2] / "contract_tests"


def _plugin_imports():
    for path in sorted(_CONTRACT_TESTS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("netbox_nso_plugin"):
                for alias in node.names:
                    yield path.name, node.lineno, node.module, alias.name


def test_contract_tests_import_only_names_the_plugin_defines():
    imports = list(_plugin_imports())
    assert imports
    missing = [
        f"{name}:{line} {module}.{attr}"
        for name, line, module, attr in imports
        if not hasattr(importlib.import_module(module), attr)
    ]
    assert missing == []
