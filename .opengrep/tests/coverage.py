# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Require every OpenGrep rule alternative to match an annotated fixture."""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULES_PATH = _REPO_ROOT / ".opengrep" / "nso-rules.yaml"
_FIXTURE_PATH = _REPO_ROOT / ".opengrep" / "tests" / "review-patterns.py"
_RULE_ID_ANNOTATION = re.compile(r"#\s*ruleid:\s*(.+?)\s*$")
_RULE_METADATA_KEYS = {
    "fix",
    "fix-regex",
    "id",
    "languages",
    "max-version",
    "message",
    "metadata",
    "min-version",
    "options",
    "paths",
    "severity",
}
_FORMULA_KEYS = {
    "focus-metavariable",
    "metavariable-analysis",
    "metavariable-comparison",
    "metavariable-name",
    "metavariable-pattern",
    "metavariable-regex",
    "pattern",
    "pattern-either",
    "pattern-inside",
    "pattern-not",
    "pattern-not-inside",
    "pattern-regex",
    "patterns",
}
_POSITIVE_KEYS = {"pattern", "pattern-inside", "pattern-regex"}
_METAVARIABLE_PATTERN_METADATA_KEYS = {"language", "metavariable"}
_RULE_FORMULA_KEYS = {"pattern", "pattern-either", "pattern-regex", "patterns"}
_RULE_CONSTRAINT_KEYS = {
    "focus-metavariable",
    "metavariable-analysis",
    "metavariable-comparison",
    "metavariable-name",
    "metavariable-regex",
    "pattern-inside",
    "pattern-not",
    "pattern-not-inside",
}


def _formula_key(node: dict, rule_id: str) -> str:
    unsupported_keys = set(node) - _FORMULA_KEYS
    if unsupported_keys:
        key = sorted(unsupported_keys)[0]
        raise ValueError(f"{rule_id}: unsupported OpenGrep operator {key!r}")
    formula_keys = set(node) & _FORMULA_KEYS
    if len(formula_keys) != 1:
        keys = ", ".join(sorted(formula_keys)) or "none"
        raise ValueError(f"{rule_id}: expected one OpenGrep operator, found {keys}")
    return next(iter(formula_keys))


def _split_metavariable_pattern(node: dict, rule_id: str) -> tuple[list[dict], bool]:
    value = node["metavariable-pattern"]
    if not isinstance(value, dict):
        raise ValueError(f"{rule_id}: metavariable-pattern must be a mapping")
    unsupported_keys = set(value) - _METAVARIABLE_PATTERN_METADATA_KEYS - _FORMULA_KEYS
    if unsupported_keys:
        key = sorted(unsupported_keys)[0]
        raise ValueError(f"{rule_id}: unsupported metavariable-pattern key {key!r}")
    formula_keys = set(value) & _FORMULA_KEYS
    if len(formula_keys) != 1:
        keys = ", ".join(sorted(formula_keys)) or "none"
        raise ValueError(f"{rule_id}: expected one metavariable-pattern operator, found {keys}")
    formula_key = next(iter(formula_keys))
    variants, has_positive_pattern = _split_formula({formula_key: value[formula_key]}, rule_id)
    split_nodes = []
    for variant in variants:
        split_value = deepcopy(value)
        split_value[formula_key] = variant[formula_key]
        split_nodes.append({"metavariable-pattern": split_value})
    return split_nodes, has_positive_pattern


def _split_formula(node: dict, rule_id: str) -> tuple[list[dict], bool]:
    if not isinstance(node, dict):
        raise ValueError(f"{rule_id}: OpenGrep formula must be a mapping")
    key = _formula_key(node, rule_id)
    value = node[key]

    if key in _POSITIVE_KEYS:
        if not isinstance(value, str):
            raise ValueError(f"{rule_id}: {key} must be a string")
        return [], True
    if key == "metavariable-pattern":
        return _split_metavariable_pattern(node, rule_id)
    if key in {"patterns", "pattern-either"}:
        if not isinstance(value, list) or not value:
            raise ValueError(f"{rule_id}: {key} must be a non-empty list")
        variants = []
        has_positive_pattern = False
        for index, child in enumerate(value):
            child_variants, child_has_positive_pattern = _split_formula(child, rule_id)
            has_positive_pattern = has_positive_pattern or child_has_positive_pattern
            if key == "pattern-either" and not child_variants:
                if not child_has_positive_pattern:
                    child_key = _formula_key(child, rule_id)
                    raise ValueError(f"{rule_id}: unsupported alternative operator {child_key!r}")
                child_variants = [deepcopy(child)]
            for child_variant in child_variants:
                split_value = deepcopy(value)
                split_value[index] = child_variant
                variants.append({key: [split_value[index]] if key == "pattern-either" else split_value})
        return variants, has_positive_pattern
    return [], False


def split_rule_alternatives(document: dict) -> list[tuple[str, int, dict]]:
    """Return one rule copy for each positive pattern alternative."""
    alternatives = []
    for rule in document["rules"]:
        rule_id = rule["id"]
        unsupported_keys = set(rule) - _RULE_METADATA_KEYS - _RULE_FORMULA_KEYS - _RULE_CONSTRAINT_KEYS
        if unsupported_keys:
            key = sorted(unsupported_keys)[0]
            raise ValueError(f"{rule_id}: unsupported OpenGrep rule key {key!r}")
        formula_keys = set(rule) & _RULE_FORMULA_KEYS
        if len(formula_keys) != 1:
            keys = ", ".join(sorted(formula_keys)) or "none"
            raise ValueError(f"{rule_id}: expected one rule operator, found {keys}")
        formula_key = next(iter(formula_keys))
        variants, has_positive_pattern = _split_formula({formula_key: rule[formula_key]}, rule_id)
        if not variants:
            if not has_positive_pattern:
                raise ValueError(f"{rule_id}: {formula_key} has no supported positive pattern")
            variants = [{formula_key: deepcopy(rule[formula_key])}]

        for alternative_index, variant in enumerate(variants, start=1):
            sub_rule = deepcopy(rule)
            sub_rule.pop("paths", None)
            sub_rule["id"] = f"{rule_id}--alt{alternative_index}"
            sub_rule[formula_key] = variant[formula_key]
            alternatives.append((rule_id, alternative_index, sub_rule))
    return alternatives


def _annotated_lines(fixture: str) -> dict[str, set[int]]:
    annotations = defaultdict(set)
    for line_number, line in enumerate(fixture.splitlines(), start=1):
        match = _RULE_ID_ANNOTATION.search(line)
        if match is None:
            continue
        for rule_id in match.group(1).split(","):
            annotations[rule_id.strip()].add(line_number + 1)
    return dict(annotations)


def _result_rule_id(check_id: str, sub_rules: dict[str, tuple[str, int]]) -> str | None:
    if check_id in sub_rules:
        return check_id
    return next((sub_rule_id for sub_rule_id in sub_rules if check_id.endswith(f".{sub_rule_id}")), None)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", type=Path, default=_RULES_PATH)
    parser.add_argument("--fixture", type=Path, default=_FIXTURE_PATH)
    parser.add_argument("--opengrep-bin", default=os.environ.get("OPENGREP_BIN", "opengrep"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Check rule and alternative coverage against the annotated fixture."""
    args = _parse_args(argv)
    document = yaml.safe_load(args.rules.read_text(encoding="utf-8"))
    fixture = args.fixture.read_text(encoding="utf-8")
    annotations = _annotated_lines(fixture)
    alternatives = split_rule_alternatives(document)
    original_rule_ids = [rule["id"] for rule in document["rules"]]
    sub_rules = {rule["id"]: (rule_id, index) for rule_id, index, rule in alternatives}

    failures = [f"{rule_id}: no # ruleid fixture annotation" for rule_id in original_rule_ids if rule_id not in annotations]
    covered = set()
    generated_document = deepcopy(document)
    generated_document["rules"] = [rule for _, _, rule in alternatives]

    with tempfile.TemporaryDirectory(prefix="opengrep-fixture-coverage-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        generated_config = temporary_path / "review-pattern-alternatives.yaml"
        generated_fixture = temporary_path / "review-patterns.py"
        generated_config.write_text(yaml.safe_dump(generated_document, sort_keys=False), encoding="utf-8")
        shutil.copyfile(args.fixture, generated_fixture)
        try:
            completed = subprocess.run(
                [args.opengrep_bin, "scan", "--config", str(generated_config), "--json", str(generated_fixture)],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            print(f"OpenGrep alternative scan failed: {error}", file=sys.stderr)
            return 1
        if completed.returncode != 0:
            print("OpenGrep alternative scan failed.", file=sys.stderr)
            if completed.stdout:
                print(completed.stdout, file=sys.stderr, end="")
            if completed.stderr:
                print(completed.stderr, file=sys.stderr, end="")
            return completed.returncode
        try:
            scan = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            print(f"OpenGrep alternative scan returned invalid JSON: {error}", file=sys.stderr)
            return 1
        if scan.get("errors"):
            print("OpenGrep alternative scan reported errors.", file=sys.stderr)
            print(json.dumps(scan["errors"], indent=2), file=sys.stderr)
            return 1
        for result in scan.get("results", []):
            sub_rule_id = _result_rule_id(result["check_id"], sub_rules)
            if sub_rule_id is None:
                continue
            rule_id, alternative_index = sub_rules[sub_rule_id]
            if result["start"]["line"] in annotations.get(rule_id, set()):
                covered.add((rule_id, alternative_index))

    failures.extend(
        f"{rule_id} alternative {alternative_index}: no fixture line matches"
        for rule_id, alternative_index, _ in alternatives
        if (rule_id, alternative_index) not in covered
    )
    if failures:
        print("\n".join(failures))
        return 1
    print(f"Fixture coverage passed for {len(original_rule_ids)} rules and {len(alternatives)} alternatives.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
