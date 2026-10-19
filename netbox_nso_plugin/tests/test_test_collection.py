# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Shared capture fixtures must not schedule another module's test methods."""

import importlib
import inspect
import unittest

import pytest


@pytest.mark.parametrize(
    "module_name",
    (
        "test_coalesced_settlement",
        "test_refusal_settlement_runtime",
        "test_refusal_settlement_families",
        "test_refusal_settlement_receipts",
        "test_settlement_e2e",
    ),
)
def test_capture_consumers_collect_only_their_own_test_methods(module_name):
    module = importlib.import_module(f"netbox_nso_plugin.tests.{module_name}")
    for value in vars(module).values():
        if not inspect.isclass(value) or not issubclass(value, unittest.TestCase):
            continue
        methods = unittest.defaultTestLoader.getTestCaseNames(value)
        if methods:
            assert value.__module__ == module.__name__, value.__name__
            assert set(methods) <= value.__dict__.keys(), value.__name__
