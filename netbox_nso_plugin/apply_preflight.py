# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Validate adapter admission evidence and project safe operator causes."""

import re
from collections.abc import Collection
from typing import Literal, NotRequired, TypedDict, get_args

from .refusal_contract import ContractError, _require, validate_refusals
from .settlement_schemas import APPLY_SKIP_REASONS, within_wire_bounds

PreflightStatus = Literal["ready", "refused", "unavailable", "error"]
StreamStatus = Literal["admitted", "blocked", "not_evaluated"]

_STOP_MESSAGES = {
    "attribution_failed": "Apply preflight failed: the refusal cannot be attributed to one stream. No stream was admitted.",
    "body_build_failed": "Apply preflight failed: the adapter could not build the device document. No stream was admitted.",
    "removal_blocked_collateral": "Apply refused: it would retract configuration outside this selection. No stream was admitted.",
    "inputs_changed_during_preflight": "Apply preflight stopped because its inputs changed during the checks. No stream was admitted. Open a fresh preview.",
}


class StreamVerdict(TypedDict):
    status: StreamStatus
    refusals: NotRequired[list[dict]]
    guard: NotRequired[dict]
    context_disturbed: NotRequired[list[str]]


class _PreflightEvidence(TypedDict):
    status: PreflightStatus
    streams: dict[str, StreamVerdict]
    baseline_refused: list[str]
    cross_stream: list[str]
    context_disturbed: list[str]
    admitted_selection: dict[str, int]
    skips: dict[str, str]
    diffs: dict[str, str]
    attempts: list[dict]
    refusals: list[dict]
    error: dict | None


class ApplyPreflight(_PreflightEvidence):
    preflight_identity: NotRequired[str]


class DeliveryPromotion(TypedDict):
    stream: str
    kind: str


class DeliveryEffect(TypedDict):
    promotes: bool
    effects: NotRequired[list[DeliveryPromotion]]


class CandidatePreflight(_PreflightEvidence):
    candidate_identity: NotRequired[str]
    plan_digest: NotRequired[str]
    delivery_effect: NotRequired[DeliveryEffect]


def _selection(raw, known):
    _require(isinstance(raw, dict), "invalid preflight selection")
    _require(set(raw) <= known, "unknown preflight stream")
    _require(all(type(value) is int and 0 < value < 2**63 for value in raw.values()), "invalid selection value")


def _stream_list(raw, known):
    _require(isinstance(raw, list) and all(isinstance(item, str) for item in raw), "invalid stream list")
    _require(len(raw) == len(set(raw)) and set(raw) <= known, "unknown or repeated stream")


def _verdict(raw, known):
    _require(isinstance(raw, dict) and "status" in raw, "missing stream verdict")
    _require(set(raw) <= {"status", "refusals", "guard", "context_disturbed"}, "unknown verdict field")
    _require(raw["status"] in get_args(StreamStatus), "unknown stream verdict")
    if "refusals" in raw:
        validate_refusals(raw["refusals"])
    if "guard" in raw:
        _require(isinstance(raw["guard"], dict), "invalid collateral guard")
        for label, keys in raw["guard"].items():
            _require(isinstance(label, str) and isinstance(keys, list), "invalid collateral keys")
            _require(all(isinstance(key, list) and key for key in keys), "invalid collateral key")
    if "context_disturbed" in raw:
        _stream_list(raw["context_disturbed"], known)
    reasons = any(raw.get(key) for key in ("refusals", "guard", "context_disturbed"))
    _require((raw["status"] == "blocked") == reasons, "stream verdict reason mismatch")


def _validate(body, selected, known, *, candidate=False):
    _require(isinstance(body, dict) and within_wire_bounds(body), "invalid preflight response")
    fields = set(_PreflightEvidence.__required_keys__)
    identities = ("candidate_identity", "plan_digest") if candidate else ("preflight_identity",)
    optional = set(identities) | ({"delivery_effect"} if candidate else set())
    _require(fields <= set(body) <= fields | optional, "invalid preflight fields")
    _require(body["status"] in get_args(PreflightStatus), "unknown preflight status")
    _selection(selected, known)
    _require(isinstance(body["streams"], dict) and set(body["streams"]) == set(selected), "stream coverage mismatch")
    for verdict in body["streams"].values():
        _verdict(verdict, known)
    for key in ("baseline_refused", "cross_stream", "context_disturbed"):
        _stream_list(body[key], known)
    _selection(body["admitted_selection"], known)
    _require(all(selected.get(key) == value for key, value in body["admitted_selection"].items()), "selection changed")
    skips = body["skips"]
    _require(isinstance(skips, dict) and set(skips) <= set(selected), "unknown skipped stream")
    _require(all(isinstance(reason, str) and reason in APPLY_SKIP_REASONS for reason in skips.values()), "unknown skip")
    admitted = {stream for stream, verdict in body["streams"].items() if verdict["status"] == "admitted"}
    _require(admitted == set(body["admitted_selection"]), "admission mismatch")
    _require(all(body["streams"][stream]["status"] == "not_evaluated" for stream in skips), "skip verdict mismatch")
    if body["status"] == "ready":
        for name in identities:
            identity = body.get(name)
            _require(
                isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity), "invalid preflight identity"
            )
        _require(
            not any(body[key] for key in ("baseline_refused", "cross_stream", "context_disturbed")), "terminal refusal"
        )
        _require(body["error"] is None, "ready result carries an error")
        _require(
            admitted | set(skips) | {key for key, value in body["streams"].items() if value["status"] == "blocked"}
            == set(selected),
            "incomplete admission",
        )
    else:
        _require(not admitted and not any(name in body for name in identities), "non-ready preflight admits streams")
    _require(isinstance(body["diffs"], dict) and set(body["diffs"]) == {"native", "cli"}, "invalid preflight diffs")
    _require(all(isinstance(value, str) for value in body["diffs"].values()), "invalid preflight diff")
    validate_refusals(body["refusals"])
    _require(
        body["error"] is None or (isinstance(body["error"], dict) and isinstance(body["error"].get("code"), str)),
        "invalid preflight diagnostic",
    )
    remaining = _validate_attempts(body["attempts"], selected, known, candidate=candidate)
    if body["status"] == "ready":
        _require(
            bool(body["attempts"])
            and not body["attempts"][-1]["dropped"]
            and remaining == confirm_selection(body, selected),
            "unproven admission",
        )
        _require(not body["refusals"], "ready preflight carries a terminal refusal")


def _validate_attempts(attempts, selected, known, *, candidate=False):
    _require(isinstance(attempts, list) and len(attempts) <= len(selected) + 1, "invalid preflight passes")
    current = dict(selected)
    for attempt in attempts:
        _require(isinstance(attempt, dict) and set(attempt) == {"selection", "dropped"}, "invalid preflight pass")
        _selection(attempt["selection"], known)
        _require(attempt["selection"] == current, "preflight pass selection changed")
        _stream_list(attempt["dropped"], set(current))
        _require(not candidate or not attempt["dropped"], "candidate stream was dropped")
        current = {key: value for key, value in current.items() if key not in attempt["dropped"]}
    return current


def validate_preflight(body, selected) -> ApplyPreflight:
    """Reject malformed or inconsistent admission evidence at the HTTP boundary."""
    from .adapter_client import AdapterError
    from .delivery import delivery_keys

    try:
        _validate(body, selected, {entry.section for entry in delivery_keys().values()})
    except ContractError as exc:
        raise AdapterError("Adapter returned invalid Apply preflight evidence.", code="invalid_response") from exc
    return body


def _delivery_effect(raw, streams):
    _require(isinstance(raw, dict) and type(raw.get("promotes")) is bool, "invalid delivery effect")
    if raw["promotes"] is False:
        _require(set(raw) == {"promotes"}, "invalid non-promoting effect")
        return
    _require(set(raw) == {"promotes", "effects"}, "invalid promoting effect")
    _require(isinstance(raw["effects"], list) and bool(raw["effects"]), "missing delivery effects")
    for effect in raw["effects"]:
        _require(isinstance(effect, dict) and set(effect) == {"stream", "kind"}, "invalid delivery promotion")
        _require(isinstance(effect["stream"], str) and effect["stream"] in streams, "unknown delivery stream")
        _require(isinstance(effect["kind"], str) and bool(effect["kind"]), "invalid delivery kind")


def validate_candidate_preflight(body, candidate_streams: Collection[str]) -> CandidatePreflight:
    """Require exact candidate coverage, both hashes and the adapter's delivery effect."""
    from .adapter_client import AdapterError
    from .delivery import delivery_keys

    try:
        known = {entry.section for entry in delivery_keys().values()}
        streams = set(candidate_streams)
        _require(bool(streams) and streams <= known, "unknown candidate stream")
        _require(isinstance(body, dict) and within_wire_bounds(body), "invalid preflight response")
        attempts = body.get("attempts")
        _require(isinstance(attempts, list), "invalid preflight passes")
        selected = dict.fromkeys(streams, 1)
        if attempts:
            _require(isinstance(attempts[0], dict), "invalid preflight pass")
            selected = attempts[0].get("selection")
            _selection(selected, known)
            _require(set(selected) == streams, "candidate selection coverage mismatch")
        _validate(body, selected, known, candidate=True)
        if body["status"] == "ready" or "delivery_effect" in body:
            _delivery_effect(body.get("delivery_effect"), streams)
    except ContractError as exc:
        raise AdapterError("Adapter returned invalid candidate preflight evidence.", code="invalid_response") from exc
    return body


def admits_intend(preflight: CandidatePreflight) -> bool:
    """Admit acquisition only when the adapter proves ordinary delivery cannot promote."""
    return preflight["status"] == "ready" and preflight.get("delivery_effect", {}).get("promotes") is False


def candidate_refusal_message(preflight: CandidatePreflight) -> str:
    """Name the adapter's delivery effects when they refuse acquisition."""
    effect = preflight.get("delivery_effect", {})
    if effect.get("promotes"):
        effects = ", ".join(f"{item['stream']}: {item['kind']}" for item in effect["effects"])
        return f"Intend refused: ordinary delivery would promote configuration ({effects})."
    return f"Intend refused: candidate preflight is {preflight['status']}."


_SKIP_TEXT = {
    "already_applied": "already applied",
    "already_authorized": "already authorized",
    "superseded": "superseded by a newer revision",
    "backfill_only": "backfill only",
    "no_receipt": "not delivered yet",
    "no_prepared_revision": "nothing prepared",
    "revision_mismatch": "revision changed",
}


def stream_verdicts(preflight):
    """Label each stream verdict with its display name, cause and skip reason."""
    from .delivery import delivery_keys

    labels = {entry.section: entry.label for entry in delivery_keys().values()}
    skips = preflight["skips"]
    return {
        stream: {
            **verdict,
            "label": labels[stream],
            "message": verdict_message(verdict),
            "skip": _SKIP_TEXT.get(skips[stream], skips[stream]) if stream in skips else "",
        }
        for stream, verdict in preflight["streams"].items()
    }


def confirm_selection(preflight, selected):
    """Keep skipped entries because Apply binds their exact reasons in the identity."""
    return {**preflight["admitted_selection"], **{key: selected[key] for key in preflight["skips"]}}


def verdict_message(verdict):
    """Render catalog causes and fixed descriptions of noncatalog blocks."""
    from .refusal_display import attributed_projection

    if verdict.get("refusals"):
        return attributed_projection(verdict["refusals"])["text"]
    if verdict.get("guard"):
        return "Apply would retract configuration outside this selection."
    if verdict.get("context_disturbed"):
        return "Apply would change the interface context of an unselected stream."
    return ""


def preflight_message(preflight):
    """Explain terminal admission without exposing adapter diagnostics."""
    if preflight["baseline_refused"]:
        return "Apply refused: the authorized baseline needs repair or Release: " + ", ".join(
            preflight["baseline_refused"]
        )
    if preflight["cross_stream"]:
        return "Apply refused: the selection causes a refusal in another stream: " + ", ".join(
            preflight["cross_stream"]
        )
    if preflight["context_disturbed"]:
        return "Apply refused: the selection changes an unselected interface context."
    if preflight["error"] is not None and preflight["error"]["code"] in _STOP_MESSAGES:
        return _STOP_MESSAGES[preflight["error"]["code"]]
    return {
        "ready": "Review the stream verdicts before Apply.",
        "refused": "Apply refused. No stream was admitted. Review the stream causes.",
        "unavailable": "Apply preflight is unavailable. No stream was admitted. Try a fresh preview.",
        "error": "Apply preflight failed. No stream was admitted. Review the adapter state and try a fresh preview.",
    }[preflight["status"]]


def request_identity(attempt):
    """Require the durable preflight that authorized this exact manual request."""
    from .adapter_client import AdapterError

    stored = attempt.preflight
    if not isinstance(stored, dict) or set(stored) != {"selected", "result"}:
        raise AdapterError("Manual Apply has no stored preflight.", code="invalid_response")
    result = validate_preflight(stored["result"], stored["selected"])
    if result["status"] != "ready" or confirm_selection(result, stored["selected"]) != attempt.selected:
        raise AdapterError("Manual Apply differs from its preflight.", code="invalid_response")
    return result["preflight_identity"]
