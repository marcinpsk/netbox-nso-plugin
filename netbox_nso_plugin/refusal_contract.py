# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Expose the shared execution contract before settlement or presentation."""

from .execution_contract import (
    _CAUSE_ORDER,
    COUNT_KEYS,
    GENERATION_FIELDS,
    GENERATION_STATUSES,
    ContractError,
    _cause,
    _key,
    _logical_identity,
    _positive,
    _require,
    _shape,
    _timestamp,
    validate_carrier_generations,
    validate_count_map,
    validate_generation,
    validate_refusals,
    validate_result,
)

__all__ = (
    "COUNT_KEYS",
    "GENERATION_FIELDS",
    "GENERATION_STATUSES",
    "ContractError",
    "_CAUSE_ORDER",
    "_cause",
    "_positive",
    "_require",
    "_shape",
    "_timestamp",
    "logical_key",
    "validate_carrier_generations",
    "validate_count_map",
    "validate_generation",
    "validate_refusals",
    "validate_result",
)


def logical_key(raw):
    """Return the hashable canonical key of one logical row identity."""
    return _key(_logical_identity(raw))
