# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Validate the shared execution contract before settlement or presentation."""

import json
import re
from datetime import datetime

from .. import refusal_protocol as protocol
from ..delivery import delivery_keys
from ..refusal_registry import OVERLAYS, SECTION_COUNTER_KEYS, RegistryError, owner_spec

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
_STREAM_FAMILIES = {
    entry.section: frozenset(
        protocol.SUBJECTS[subject].family
        for spec in OVERLAYS.values()
        if spec.delivery_keys == (scope,)
        for subject in spec.subjects
    )
    for scope, entry in delivery_keys().items()
}


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
    _require(
        protocol.SUBJECTS[raw["row"]["subject"]].family in _STREAM_FAMILIES.get(raw["stream"], ()),
        "invalid logical stream family",
    )
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


def _key(raw):
    return json.dumps(raw, sort_keys=True, separators=(",", ":"))


def _logical_identity(raw):
    return {key: raw[key] for key in _IDENTITY_FIELDS}


def _counter_key(outcome):
    try:
        spec = owner_spec(outcome["carrier"]["row"]["subject"])
    except RegistryError:
        raise ContractError("unknown settlement carrier") from None
    if len(spec.counter_keys) == 1:
        return spec.counter_keys[0]
    counter = f"{outcome['carrier']['stream']}_count_by_outcome"
    _require(counter in spec.counter_keys, "invalid settlement carrier stream")
    return counter


def _validate_carriers(result, outcomes):
    carriers = result["settlement_owners"]
    _require(isinstance(carriers, list), "invalid settlement owners")
    grouped = {}
    priority = {"in_sync": 0, "not_applied": 1, "unavailable": 2, "refused": 3}
    for outcome in outcomes:
        identity = _key(outcome["carrier"])
        grouped.setdefault(identity, []).append(outcome)
    seen = set()
    expected_counts = {}
    for carrier in carriers:
        _shape(carrier, _CARRIER_FIELDS | {"disposition"})
        public = {key: carrier[key] for key in _CARRIER_FIELDS}
        _identity(public, carrier=True)
        identity = _key(public)
        _require(identity not in seen and identity in grouped, "invalid settlement carrier membership")
        seen.add(identity)
        members = grouped[identity]
        disposition = max((row["disposition"] for row in members), key=priority.__getitem__)
        _require(carrier["disposition"] == disposition, "settlement carrier disposition mismatch")
        counter = _counter_key(members[0])
        counts = expected_counts.setdefault(counter, dict.fromkeys(COUNT_KEYS, 0))
        counts["apply_failed" if disposition == "refused" else disposition] += 1
    _require(seen == set(grouped), "missing settlement carrier")
    counters = {key: value for key, value in result.items() if key.endswith("_count_by_outcome")}
    _require(set(expected_counts).issubset(counters), "missing carrier outcome count")
    for key, counts in counters.items():
        validate_count_map(counts)
        _require(counts == expected_counts.get(key, dict.fromkeys(COUNT_KEYS, 0)), "carrier outcome count mismatch")


def _validate_refusal_links(result, outcomes):
    logicals = {_key(_logical_identity(row)): row for row in outcomes}
    linked = {identity: set() for identity in logicals}
    seen = set()
    for cause in result["refusals"]:
        _cause(cause, attributed=True)
        _require(
            cause["sender_phase"] != "post_commit_verification" or result["commit_acknowledged"],
            "verification refusal without commit acknowledgement",
        )
        signature = _key(cause)
        _require(signature not in seen, "duplicate attributed refusal")
        seen.add(signature)
        catalog = _key({key: cause[key] for key in _CAUSE_FIELDS})
        for logical in cause["logical_rows"]:
            identity = _key(logical)
            if cause["provenance"] == "current":
                _require(identity in logicals, "refusal names an unattempted logical row")
            if identity in linked:
                linked[identity].add(catalog)
    for identity, outcome in logicals.items():
        causes = {_key(cause) for cause in outcome["refusals"]}
        _require(len(causes) == len(outcome["refusals"]), "duplicate logical refusal")
        _require(causes.issubset(linked[identity]), "logical refusal lacks attributed evidence")
        if outcome["disposition"] == "refused":
            _require(bool(causes), "refused logical row has no catalog cause")
        else:
            _require(not causes, "non-refused logical row carries refusal causes")


def validate_refusals(raw):
    """Validate attributed catalog records without interpreting device text."""
    _require(isinstance(raw, list), "invalid attributed refusals")
    for cause in raw:
        _cause(cause, attributed=True)
    return raw


def validate_result(result, *, sections=()) -> dict:
    """Require complete logical membership, catalog causes, and carrier-derived counts."""
    _require(isinstance(result, dict) and all(isinstance(key, str) for key in result), "invalid execution result")
    _require(
        {"row_outcomes", "refusals", "settlement_owners", "commit_acknowledged"}.issubset(result),
        "missing structured execution evidence",
    )
    _require(type(result["commit_acknowledged"]) is bool, "invalid commit acknowledgement")
    outcomes = result["row_outcomes"]
    _require(isinstance(outcomes, list) and isinstance(result["refusals"], list), "invalid execution projections")
    seen = set()
    for outcome in outcomes:
        _outcome(outcome)
        logical = (outcome["stream"], outcome["row"]["subject"], outcome["row"]["key"])
        _require(logical not in seen, "duplicate logical outcome")
        seen.add(logical)
        _require(
            not result["commit_acknowledged"] or outcome["disposition"] != "refused",
            "committed execution cannot claim a rollback refusal",
        )
    for section in sections:
        _require(section in SECTION_COUNTER_KEYS, "invalid counted section")
        _require(set(SECTION_COUNTER_KEYS[section]).issubset(result), "missing promised outcome count map")
    _validate_carriers(result, outcomes)
    _validate_refusal_links(result, outcomes)
    return result


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
    _require(
        raw["carrier_job_status"] != "succeeded" or isinstance(raw["carrier_job_result"], dict),
        "succeeded carrier without result",
    )
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
        validate_result(result, sections=raw["sections"])
        for outcome in result["row_outcomes"]:
            if outcome["generation_id"] != raw["generation_id"]:
                continue
            stream = outcome["stream"]
            _require(
                outcome["frozen_revision"] == raw["stream_revisions"].get(stream)
                and stream in raw["stream_revisions"]
                and stream in raw["source_push_seq"]
                and outcome["source_push_seq"] == raw["source_push_seq"][stream],
                "logical generation provenance mismatch",
            )
    return raw


def validate_carrier_generations(generations):
    """Require every logical and carrier identity to have its own generation authority."""
    groups = {}
    for generation in generations:
        validate_generation(generation)
        if generation["row_outcomes"] is None:
            continue
        authorities = groups.setdefault(generation["carrier_job_id"], {})
        _require(generation["generation_id"] not in authorities, "duplicate generation authority")
        authorities[generation["generation_id"]] = generation
    for authorities in groups.values():
        first = next(iter(authorities.values()))
        result = first["carrier_job_result"]
        for authority in authorities.values():
            _require(
                all(
                    authority[key] == first[key]
                    for key in ("carrier_job_status", "carrier_job_result", "carrier_job_error")
                ),
                "coalesced carrier certificate mismatch",
            )
        for outcome in result["row_outcomes"]:
            for identity in (outcome, outcome["carrier"]):
                authority = authorities.get(identity["generation_id"])
                _require(authority is not None, "missing generation authority")
                stream = identity["stream"]
                _require(
                    stream in authority["stream_revisions"]
                    and stream in authority["source_push_seq"]
                    and identity["frozen_revision"] == authority["stream_revisions"][stream]
                    and identity["source_push_seq"] == authority["source_push_seq"][stream],
                    "carrier generation provenance mismatch",
                )
    return generations
