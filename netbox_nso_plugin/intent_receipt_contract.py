# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Validate the adapter's retained intent receipt surface at the HTTP boundary."""

import re

from .delivery import delivery_keys
from .refusal_contract import _positive, _require, _shape, _timestamp

RECEIPT_FIELDS = frozenset(
    {
        "device_id",
        "section",
        "push_seq",
        "request_digest",
        "store_only",
        "delete_origin",
        "backfill_only",
        "status_code",
        "response",
        "generation_id",
        "created_at",
        "updated_at",
    }
)


def _receipt(raw):
    _shape(raw, RECEIPT_FIELDS)
    _require(_positive(raw["device_id"]) and _positive(raw["push_seq"]), "invalid receipt identity")
    streams = {entry.section for entry in delivery_keys().values() if entry.in_protocol}
    _require(isinstance(raw["section"], str) and raw["section"] in streams, "invalid receipt stream")
    _require(
        isinstance(raw["request_digest"], str) and re.fullmatch(r"[0-9a-f]{64}", raw["request_digest"]) is not None,
        "invalid receipt digest",
    )
    _require(
        all(type(raw[name]) is bool for name in ("store_only", "delete_origin", "backfill_only")),
        "invalid receipt modes",
    )
    _require(type(raw["status_code"]) is int and 100 <= raw["status_code"] <= 599, "invalid receipt status")
    _require(raw["response"] is None or isinstance(raw["response"], dict), "invalid receipt response")
    _require(raw["generation_id"] is None or _positive(raw["generation_id"]), "invalid receipt generation")
    _timestamp(raw["created_at"])
    if raw["updated_at"] is not None:
        _timestamp(raw["updated_at"])


def validate_intent_receipts(raw):
    """Return the complete receipt document after its exact wire shape is proved."""
    _shape(raw, {"receipts", "global_max_push_seq", "global_max_route_id"})
    _require(isinstance(raw["receipts"], list), "invalid receipt collection")
    for name in ("global_max_push_seq", "global_max_route_id"):
        _require(raw[name] is None or _positive(raw[name]), "invalid receipt watermark")
    identities = set()
    for receipt in raw["receipts"]:
        _receipt(receipt)
        identity = receipt["device_id"], receipt["section"]
        _require(identity not in identities, "duplicate receipt identity")
        identities.add(identity)
    return raw
