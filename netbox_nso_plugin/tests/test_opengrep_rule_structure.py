# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Require OpenGrep rules to cover supported imported-symbol reference forms."""

from __future__ import annotations

import ast
import importlib.util
import keyword
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from django.test import SimpleTestCase

_RULES_PATH = Path(__file__).resolve().parents[2] / ".opengrep" / "nso-rules.yaml"
_ROOT = _RULES_PATH.parents[1]
_FIXTURE_PATH = Path(__file__).resolve().parents[2] / ".opengrep" / "tests" / "review-patterns.py"
_ADAPTER_ERROR_CHECKER = _RULES_PATH.parent / "check-adapter-error-continue.py"
_RESUME_GUIDANCE_CHECKER = _RULES_PATH.parent / "check-resume-failure-guidance.py"
_MONOTONIC_RULE_ID = "nso-global-monotonic-patch"
_COVERAGE_PATH = _RULES_PATH.parent / "tests" / "coverage.py"
_PATTERN_KEYS = {"pattern", "pattern-inside", "pattern-not", "pattern-not-inside"}
_PATTERN_LIST_KEYS = {"patterns", "pattern-either"}
_ROOT_NAME = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)(?=\s*[.(])")
# opengrep matches the method receiver literally, so "self" is never an import root.
_ALLOWED_ROOT_NAMES = frozenset({"self"})

_COVERAGE_SPEC = importlib.util.spec_from_file_location("_opengrep_coverage", _COVERAGE_PATH)
assert _COVERAGE_SPEC is not None
assert _COVERAGE_SPEC.loader is not None
_COVERAGE = importlib.util.module_from_spec(_COVERAGE_SPEC)
_COVERAGE_SPEC.loader.exec_module(_COVERAGE)


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
        elif key in _PATTERN_LIST_KEYS or (key in _PATTERN_KEYS and isinstance(value, (dict, list))):
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
            for name in names - _ALLOWED_ROOT_NAMES
            if not _is_importable(name) and not _has_qualified_pattern(name, patterns)
        )
    return sorted(violations), scanned_names


def _rule_violations(rules_path: Path) -> list[str]:
    return _scan_rules(rules_path)[0]


def _run_checker(checker: Path, mode: str, *paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(checker), mode, *(str(path) for path in paths)],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        check=False,
    )


_CHECKER_CASES = (
    (
        _ADAPTER_ERROR_CHECKER,
        "nso-adapter-error-after-continue",
        """from netbox_nso_plugin.adapter_client import AdapterError

def validate(items):
    for item in items:
        if item is None:
            continue
        raise AdapterError("invalid")
""",
    ),
    (
        _RESUME_GUIDANCE_CHECKER,
        "nso-resume-failure-guidance",
        """from netbox_nso_plugin.deployment import resume

def recover():
    resume()
""",
    ),
)


class TestReviewPatternCheckerPaths(SimpleTestCase):
    def test_scan_expands_and_deduplicates_explicit_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for checker, rule_id, violation in _CHECKER_CASES:
                with self.subTest(checker=checker.name):
                    case_root = root / checker.stem
                    finding_path = case_root / "nested" / "finding.py"
                    finding_path.parent.mkdir(parents=True)
                    finding_path.write_text(violation, encoding="utf-8")
                    clean_path = case_root / "clean.py"
                    clean_path.write_text("def clean():\n    return None\n", encoding="utf-8")

                    result = _run_checker(checker, "scan", clean_path, case_root, finding_path)

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(str(finding_path), result.stdout)
                    self.assertEqual(result.stdout.count(rule_id), 1)

                    clean = _run_checker(checker, "scan", clean_path)
                    self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)

    def test_scan_without_paths_keeps_each_default_scope_clean(self):
        for checker, _rule_id, _violation in _CHECKER_CASES:
            with self.subTest(checker=checker.name):
                result = _run_checker(checker, "scan")

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_scan_rejects_invalid_explicit_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_path = root / "missing"
            non_python_path = root / "notes.txt"
            non_python_path.write_text("not Python\n", encoding="utf-8")

            for checker, _rule_id, _violation in _CHECKER_CASES:
                for invalid_path in (missing_path, non_python_path):
                    with self.subTest(checker=checker.name, path=invalid_path):
                        result = _run_checker(checker, "scan", invalid_path)

                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertIn(str(invalid_path), result.stderr)

    def test_test_mode_requires_exactly_one_fixture(self):
        for checker, _rule_id, _violation in _CHECKER_CASES:
            with self.subTest(checker=checker.name):
                valid = _run_checker(checker, "test", _FIXTURE_PATH)
                missing = _run_checker(checker, "test")
                extra = _run_checker(checker, "test", _FIXTURE_PATH, _FIXTURE_PATH)

                self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
                self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
                self.assertEqual(extra.returncode, 2, extra.stdout + extra.stderr)


class TestOpenGrepRuleStructure(SimpleTestCase):
    def test_rule_roots_resolve_or_have_module_qualified_variants(self):
        violations = _rule_violations(_RULES_PATH)

        self.assertEqual(violations, [], "\n".join(violations))

    def test_rule_scan_finds_distinct_root_names(self):
        _, scanned_names = _scan_rules(_RULES_PATH)

        self.assertGreaterEqual(len(scanned_names), 4)

    def test_rule_scan_reaches_names_nested_under_a_negative_pattern(self):
        rule = {
            "id": "nested-shape",
            "patterns": [
                {"pattern-not": {"patterns": [{"pattern": "nestedpatterns.call(...)"}]}},
                {"pattern-not-inside": {"pattern-either": [{"pattern": "nestedeither.call(...)"}]}},
                {"pattern-not": [{"pattern": "nestedlist.call(...)"}]},
            ],
        }

        names = _root_names(list(_pattern_texts(rule)))

        self.assertIn("nestedpatterns", names)
        self.assertIn("nestedeither", names)
        self.assertIn("nestedlist", names)

    def test_monotonic_rule_fixture_uses_the_directly_imported_patch_form(self):
        source = _FIXTURE_PATH.read_text(encoding="utf-8")
        annotated = {
            lineno + 1
            for lineno, line in enumerate(source.splitlines(), start=1)
            if line.strip() == f"# ruleid: {_MONOTONIC_RULE_ID}"
        }
        tree = ast.parse(source, filename=str(_FIXTURE_PATH))
        calls = {node.lineno: node for node in ast.walk(tree) if isinstance(node, ast.Call)}

        self.assertGreaterEqual(len(annotated), 2)
        for lineno in sorted(annotated):
            self.assertIn(lineno, calls)
            call = calls[lineno]
            self.assertIsInstance(call.func, ast.Name)
            self.assertEqual(call.func.id, "patch")

        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "unittest.mock"
            for alias in node.names
        }
        self.assertIn("patch", imported)

    def test_adding_a_bare_name_alternative_is_rejected(self):
        document = {
            "rules": [
                {
                    "id": _MONOTONIC_RULE_ID,
                    "languages": ["python"],
                    "severity": "ERROR",
                    "message": "Patch a module-local clock wrapper.",
                    "patterns": [
                        {
                            "pattern-either": [
                                {"pattern": "unittest.mock.patch($TARGET, ...)"},
                                {"pattern": "patch($TARGET, ...)"},
                            ]
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / "nso-rules.yaml"
            rules_path.write_text(yaml.safe_dump(document), encoding="utf-8")

            self.assertEqual(_rule_violations(rules_path), [f"{_MONOTONIC_RULE_ID}: patch"])


class TestOpenGrepAlternativeCoverage(SimpleTestCase):
    def test_generated_sub_rules_drop_path_filters(self):
        document = {
            "rules": [
                {
                    "id": "example",
                    "pattern-either": [
                        {"pattern": "first(...)"},
                        {"pattern": "second(...)"},
                    ],
                    "paths": {"include": ["netbox_nso_plugin/tests/**"]},
                }
            ]
        }

        alternatives = _COVERAGE.split_rule_alternatives(document)

        self.assertTrue(all("paths" not in rule for _, _, rule in alternatives))

    def test_nested_top_level_alternatives_yield_one_sub_rule_per_leaf(self):
        document = {
            "rules": [
                {
                    "id": "example",
                    "pattern-either": [
                        {"pattern": "first(...)"},
                        {
                            "patterns": [
                                {
                                    "pattern-either": [
                                        {"pattern": "second(...)"},
                                        {"pattern": "def $F(:"},
                                    ]
                                },
                                {"pattern-not": "excluded(...)"},
                            ]
                        },
                    ],
                }
            ]
        }

        alternatives = _COVERAGE.split_rule_alternatives(document)

        self.assertEqual(len(alternatives), 3)
        self.assertEqual(
            [rule["pattern-either"] for _, _, rule in alternatives],
            [
                [{"pattern": "first(...)"}],
                [
                    {
                        "patterns": [
                            {"pattern-either": [{"pattern": "second(...)"}]},
                            {"pattern-not": "excluded(...)"},
                        ]
                    }
                ],
                [
                    {
                        "patterns": [
                            {"pattern-either": [{"pattern": "def $F(:"}]},
                            {"pattern-not": "excluded(...)"},
                        ]
                    }
                ],
            ],
        )

    def test_metavariable_pattern_alternatives_keep_outer_pattern(self):
        document = {
            "rules": [
                {
                    "id": "example",
                    "patterns": [
                        {"pattern": "outer($VALUE)"},
                        {
                            "metavariable-pattern": {
                                "metavariable": "$VALUE",
                                "pattern-either": [
                                    {"pattern": "first(...)"},
                                    {"pattern-regex": "second\\(.*"},
                                ],
                            }
                        },
                    ],
                }
            ]
        }

        alternatives = _COVERAGE.split_rule_alternatives(document)

        self.assertEqual(len(alternatives), 2)
        self.assertTrue(all(rule["patterns"][0] == {"pattern": "outer($VALUE)"} for _, _, rule in alternatives))
        self.assertEqual(
            [rule["patterns"][1]["metavariable-pattern"]["pattern-either"] for _, _, rule in alternatives],
            [
                [{"pattern": "first(...)"}],
                [{"pattern-regex": "second\\(.*"}],
            ],
        )

    def test_unsupported_alternative_operator_names_rule_and_key(self):
        document = {
            "rules": [
                {
                    "id": "unsupported-example",
                    "pattern-either": [
                        {"pattern": "first(...)"},
                        {"pattern-not-regex": "excluded"},
                    ],
                }
            ]
        }

        with self.assertRaises(ValueError) as context:
            _COVERAGE.split_rule_alternatives(document)

        self.assertIn("unsupported-example", str(context.exception))
        self.assertIn("pattern-not-regex", str(context.exception))

    def test_a_rule_without_a_positive_pattern_aborts_the_split(self):
        document = {"rules": [{"id": "negative-only", "patterns": [{"pattern-not": "excluded(...)"}]}]}

        with self.assertRaises(ValueError) as context:
            _COVERAGE.split_rule_alternatives(document)

        self.assertIn("negative-only", str(context.exception))
        self.assertIn("no supported positive pattern", str(context.exception))

    def test_top_level_pattern_either_yields_one_sub_rule_per_branch(self):
        document = {
            "rules": [
                {
                    "id": "example",
                    "pattern-either": [
                        {"pattern": "first(...)"},
                        {"pattern": "second(...)"},
                        {"pattern": "third(...)"},
                    ],
                    "pattern-not-inside": "ignored(...)",
                }
            ]
        }

        alternatives = _COVERAGE.split_rule_alternatives(document)

        self.assertEqual(
            [(rule_id, index) for rule_id, index, _ in alternatives],
            [("example", 1), ("example", 2), ("example", 3)],
        )
        self.assertEqual(
            [rule["id"] for _, _, rule in alternatives],
            ["example--alt1", "example--alt2", "example--alt3"],
        )
        self.assertEqual(
            [rule["pattern-either"] for _, _, rule in alternatives],
            [
                [{"pattern": "first(...)"}],
                [{"pattern": "second(...)"}],
                [{"pattern": "third(...)"}],
            ],
        )
        self.assertTrue(all(rule["pattern-not-inside"] == "ignored(...)" for _, _, rule in alternatives))

    def test_nested_pattern_either_keeps_other_patterns_entries(self):
        document = {
            "rules": [
                {
                    "id": "example",
                    "patterns": [
                        {"pattern-inside": "def wrapper():\n    ..."},
                        {
                            "pattern-either": [
                                {"pattern": "first(...)"},
                                {"pattern": "second(...)"},
                            ]
                        },
                        {"pattern-not": "excluded(...)"},
                    ],
                }
            ]
        }

        alternatives = _COVERAGE.split_rule_alternatives(document)

        self.assertEqual(len(alternatives), 2)
        self.assertEqual(
            [rule["patterns"][1] for _, _, rule in alternatives],
            [
                {"pattern-either": [{"pattern": "first(...)"}]},
                {"pattern-either": [{"pattern": "second(...)"}]},
            ],
        )
        self.assertTrue(
            all(
                rule["patterns"][0] == {"pattern-inside": "def wrapper():\n    ..."}
                and rule["patterns"][2] == {"pattern-not": "excluded(...)"}
                for _, _, rule in alternatives
            )
        )

    def test_single_pattern_yields_one_sub_rule(self):
        document = {"rules": [{"id": "example", "pattern": "target(...)"}]}

        alternatives = _COVERAGE.split_rule_alternatives(document)

        self.assertEqual(len(alternatives), 1)
        self.assertEqual(alternatives[0][0:2], ("example", 1))
        self.assertEqual(alternatives[0][2]["pattern"], "target(...)")

    def test_current_rules_each_yield_an_alternative(self):
        document = yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8"))

        alternatives = _COVERAGE.split_rule_alternatives(document)
        covered_rule_ids = {rule_id for rule_id, _, _ in alternatives}

        self.assertEqual(covered_rule_ids, {rule["id"] for rule in document["rules"]})
