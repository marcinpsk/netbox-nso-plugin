# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require OpenGrep rules to cover supported imported-symbol reference forms."""

from __future__ import annotations

import importlib.util
import keyword
import re
from pathlib import Path

import yaml
from django.test import SimpleTestCase

_RULES_PATH = Path(__file__).resolve().parents[2] / ".opengrep" / "nso-rules.yaml"
_PATTERN_KEYS = {"pattern", "pattern-inside", "pattern-not", "pattern-not-inside"}
_PATTERN_LIST_KEYS = {"patterns", "pattern-either"}
_ROOT_NAME = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)(?=\s*[.(])")


def _pattern_texts(node):
    if isinstance(node, list):
        for item in node:
            yield from _pattern_texts(item)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key in _PATTERN_KEYS and isinstance(value, str):
            yield value
        elif key in _PATTERN_LIST_KEYS:
            yield from _pattern_texts(value)


def _root_names(patterns: list[str]) -> set[str]:
    return {name for pattern in patterns for name in _ROOT_NAME.findall(pattern) if not keyword.iskeyword(name)}


def _is_importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def _has_qualified_pattern(name: str, patterns: list[str]) -> bool:
    qualified_name = re.compile(rf"\$[A-Za-z_]\w*\.{re.escape(name)}\b")
    return any(qualified_name.search(pattern) for pattern in patterns)


def _scan_rules(rules_path: Path) -> tuple[list[str], set[str]]:
    document = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    violations = []
    scanned_names = set()
    for rule in document["rules"]:
        patterns = list(_pattern_texts(rule))
        names = _root_names(patterns)
        scanned_names.update(names)
        violations.extend(
            f"{rule['id']}: {name}"
            for name in names
            if not _is_importable(name) and not _has_qualified_pattern(name, patterns)
        )
    return sorted(violations), scanned_names


def _rule_violations(rules_path: Path) -> list[str]:
    return _scan_rules(rules_path)[0]


class TestOpenGrepRuleStructure(SimpleTestCase):
    def test_rule_roots_resolve_or_have_module_qualified_variants(self):
        violations = _rule_violations(_RULES_PATH)

        self.assertEqual(violations, [], "\n".join(violations))

    def test_rule_scan_finds_distinct_root_names(self):
        _, scanned_names = _scan_rules(_RULES_PATH)

        self.assertGreaterEqual(len(scanned_names), 4)
