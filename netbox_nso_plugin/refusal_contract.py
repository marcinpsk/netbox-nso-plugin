# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Pure C-schema validation, installed before the settlement consumer cutover."""

import json
import re
from datetime import datetime

from . import refusal_protocol as protocol
from .refusal_registry import SECTION_COUNTER_KEYS

COUNT_KEYS = frozenset({"in_sync", "apply_failed", "not_applied", "unavailable"})
GENERATION_FIELDS = frozenset(
    {
        "generation_id",
        "seq",
        "status",
        "sections",
        "stream_revisions",
        "source_push_seq",
        "carrier_job_id",
        "carrier_job_status",
        "carrier_job_result",
        "carrier_job_error",
        "updated_at",
        "row_outcomes",
        "refusals",
    }
)
GENERATION_STATUSES = frozenset({"pending", "running", "settled", "failed", "outcome_unknown", "abandoned"})
_IDENTITY_FIELDS = frozenset(
    {
        "stream",
        "row",
        "source_push_seq",
        "frozen_revision",
        "acceptance_identity",
        "generation_id",
        "settlement_cohort",
        "carrier",
    }
)
_CARRIER_FIELDS = (_IDENTITY_FIELDS - {"acceptance_identity", "carrier"}) | {"accepted_at"}
_CAUSE_ORDER = ("family", "row", "field", "reason", "message_id", "message", "related")
_CAUSE_FIELDS = frozenset(_CAUSE_ORDER)


class ContractError(ValueError):
    """Evidence violates the closed contract; messages contain no response values."""


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _shape(raw, fields):
    _require(isinstance(raw, dict) and set(raw) == fields, "invalid contract shape")


def _positive(value):
    return type(value) is int and value > 0


def _timestamp(value):
    _require(isinstance(value, str), "invalid timestamp type")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError("invalid timestamp") from None
    _require(parsed.utcoffset() is not None, "timestamp must be aware")


def validate_count_map(raw) -> dict:
    _shape(raw, COUNT_KEYS)
    _require(all(type(value) is int and value >= 0 for value in raw.values()), "invalid outcome count")
    return raw


def _row(raw):
    _shape(raw, {"subject", "key"})
    _require(isinstance(raw["subject"], str) and raw["subject"] in protocol.SUBJECTS, "invalid row subject")
    _require(
        isinstance(raw["key"], str) and re.fullmatch(r"[0-9a-f]{64}", raw["key"]) is not None, "invalid row digest"
    )


def _identity(raw, *, carrier=False):
    _shape(raw, _CARRIER_FIELDS if carrier else _IDENTITY_FIELDS)
    _require(isinstance(raw["stream"], str) and bool(raw["stream"]), "invalid stream")
    _row(raw["row"])
    for key in ("source_push_seq", "frozen_revision", "generation_id", "settlement_cohort"):
        _require(raw[key] is None or _positive(raw[key]), "invalid contributor identity")
    acceptance = raw["accepted_at" if carrier else "acceptance_identity"]
    if acceptance is not None:
        _timestamp(acceptance)
    if not carrier:
        _identity(raw["carrier"], carrier=True)


def _cause(raw, *, attributed=False):
    extra = {"logical_rows", "provenance", "sender_phase"} if attributed else set()
    _shape(raw, _CAUSE_FIELDS | extra)
    catalog = {key: raw[key] for key in _CAUSE_ORDER}
    _row(raw["row"])
    catalog["row"] = {key: raw["row"][key] for key in ("subject", "key")}
    if raw["related"] is not None:
        _shape(raw["related"], {"family", "row", "field"})
        catalog["related"] = {key: raw["related"][key] for key in ("family", "row", "field")}
        related_row = raw["related"]["row"]
        if related_row is not None:
            _row(related_row)
            catalog["related"]["row"] = {key: related_row[key] for key in ("subject", "key")}
    try:
        protocol.parse_refusals(protocol.PREFIX + json.dumps({"refusals": [catalog]}, separators=(",", ":")))
    except (protocol.RefusalProtocolError, TypeError, ValueError):
        raise ContractError("invalid catalog refusal") from None
    if attributed:
        _require(raw["provenance"] in ("current", "retained", "prior"), "invalid refusal provenance")
        _require(
            raw["sender_phase"] in ("pre_commit_dry_run", "commit", "post_commit_verification"), "invalid refusal phase"
        )
        _require(isinstance(raw["logical_rows"], list), "invalid refusal logical rows")
        for row in raw["logical_rows"]:
            _identity(row)


def _outcome(raw):
    _shape(raw, _IDENTITY_FIELDS | {"disposition", "refusals"})
    _identity({key: raw[key] for key in _IDENTITY_FIELDS})
    _require(raw["disposition"] in ("in_sync", "refused", "not_applied", "unavailable"), "invalid disposition")
    _require(isinstance(raw["refusals"], list), "invalid row refusals")
    for cause in raw["refusals"]:
        _cause(cause)


def validate_generation(raw) -> dict:
    _shape(raw, GENERATION_FIELDS)
    _require(_positive(raw["generation_id"]) and _positive(raw["seq"]), "invalid generation identity")
    _require(isinstance(raw["status"], str) and raw["status"] in GENERATION_STATUSES, "invalid generation status")
    _timestamp(raw["updated_at"])
    _require(
        isinstance(raw["sections"], list) and all(isinstance(item, str) for item in raw["sections"]), "invalid sections"
    )
    for field, nullable in (("stream_revisions", False), ("source_push_seq", True)):
        mapping = raw[field]
        _require(
            isinstance(mapping, dict)
            and all(
                isinstance(key, str) and (_positive(value) or (nullable and value is None))
                for key, value in mapping.items()
            ),
            "invalid stream provenance",
        )
    _require(raw["carrier_job_id"] is None or _positive(raw["carrier_job_id"]), "invalid carrier identity")
    _require(
        raw["carrier_job_status"] is None or raw["carrier_job_status"] in ("queued", "running", "succeeded", "failed"),
        "invalid carrier status",
    )
    for field in ("carrier_job_result", "carrier_job_error"):
        _require(raw[field] is None or isinstance(raw[field], dict), "invalid carrier snapshot")
    if raw["status"] == "settled":
        _require(
            raw["carrier_job_status"] == "succeeded"
            and raw["carrier_job_id"] is not None
            and isinstance(raw["carrier_job_result"], dict),
            "invalid settled carrier",
        )
    result = raw["carrier_job_result"]
    for field, validator in (("row_outcomes", _outcome), ("refusals", lambda item: _cause(item, attributed=True))):
        projection = raw[field]
        _require(projection is None or isinstance(projection, list), "invalid typed projection")
        if result is not None:
            _require(
                field in result and result[field] == projection and isinstance(projection, list),
                "carrier projection mismatch",
            )
        else:
            _require(projection is None, "projection without carrier result")
        if projection is not None:
            for record in projection:
                validator(record)
    if result is not None:
        for section in raw["sections"]:
            _require(section in SECTION_COUNTER_KEYS, "invalid counted section")
            for key in SECTION_COUNTER_KEYS[section]:
                _require(key in result, "missing promised outcome count map")
        for key, counts in result.items():
            if key.endswith("_count_by_outcome"):
                validate_count_map(counts)
    return raw
