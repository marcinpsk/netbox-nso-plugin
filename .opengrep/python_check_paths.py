# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Expand Python checker scan paths."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def scan_python_paths(paths: list[Path], defaults: Iterable[Path]) -> tuple[Path, ...]:
    for path in paths:
        if path.is_dir() or (path.is_file() and path.suffix == ".py"):
            continue
        raise ValueError(f"invalid Python scan path: {path}")

    expanded = []
    for path in paths or defaults:
        if path.is_dir():
            expanded.extend(path.rglob("*.py"))
        elif path.suffix == ".py":
            expanded.append(path)
    deduplicated = {path.resolve(): path for path in expanded}
    return tuple(sorted(deduplicated.values(), key=str))
