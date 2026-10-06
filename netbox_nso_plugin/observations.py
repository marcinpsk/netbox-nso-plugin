# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Validate and copy immutable family observation documents."""

import hashlib
import json
from datetime import datetime

OBSERVATION_ATTRIBUTES = {
    "interface_attributes": ("description", "enabled"),
    "interface_ip": ("address", "prefix_length", "secondary", "vrf"),
}


class ObservationProtocolError(ValueError):
    """The observation does not satisfy the family publication contract."""


def _require(condition, reason):
    if not condition:
        raise ObservationProtocolError(f"Invalid observation: {reason}.")


def _nullable_string(value):
    return value is None or isinstance(value, str)


def _validate_interface(item):
    fields = ("description", "kind", "parent_binding", "encap_tag", "vrf", "service")
    _require(isinstance(item, dict) and set(item) == {"name", "enabled", *fields}, "interface fields")
    _require(isinstance(item["name"], str) and bool(item["name"]), "interface name")
    _require(all(_nullable_string(item[field]) for field in fields), "interface values")
    _require(item["enabled"] is None or type(item["enabled"]) is bool, "interface enabled")


def _validate_ip_interface(item):
    _require(isinstance(item, dict) and set(item) == {"interface", "bound_port", "addresses"}, "IP interface fields")
    _require(isinstance(item["interface"], str) and bool(item["interface"]), "IP interface name")
    _require(_nullable_string(item["bound_port"]) and isinstance(item["addresses"], list), "IP interface values")
    for address in item["addresses"]:
        _require(
            isinstance(address, dict) and set(address) == {"address", "prefix_length", "family", "secondary", "vrf"},
            "address fields",
        )
        _require(isinstance(address["address"], str) and isinstance(address["family"], str), "address values")
        _require(address["prefix_length"] is None or type(address["prefix_length"]) is int, "prefix length")
        _require(type(address["secondary"]) is bool and _nullable_string(address["vrf"]), "address attributes")


def _validate_document(family, document):
    _require(isinstance(document, dict) and set(document) == {"interfaces", "unprojectable"}, "document fields")
    _require(isinstance(document["interfaces"], list) and isinstance(document["unprojectable"], list), "document lists")
    validate = _validate_interface if family == "interface_attributes" else _validate_ip_interface
    for item in document["interfaces"]:
        validate(item)
    for item in document["unprojectable"]:
        _require(
            isinstance(item, dict)
            and set(item) == {"index", "reason"}
            and type(item["index"]) is int
            and item["index"] >= 0
            and isinstance(item["reason"], str),
            "unprojectable entry",
        )


def observation_defaults(family, revision, source_epoch, observation):
    """Return an independent snapshot bound to the admitted payload identity."""
    _require(isinstance(observation, dict), "missing document")
    _require(observation.get("family") == family, "family mismatch")
    for field, expected in (("revision", revision), ("source_epoch", source_epoch)):
        _require(
            type(expected) is int and type(observation.get(field)) is int and observation[field] == expected,
            f"{field} mismatch",
        )
    coverage = observation.get("coverage")
    _require(coverage == {"attributes": list(OBSERVATION_ATTRIBUTES[family])}, "coverage")
    document = observation.get("document")
    _validate_document(family, document)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    _require(observation.get("digest") == digest, "digest mismatch")
    try:
        observed_at = datetime.fromisoformat(observation["observed_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ObservationProtocolError("Invalid observation: observed_at.") from exc
    _require(observed_at.utcoffset() is not None, "observed_at needs a UTC offset")
    return {
        "revision": revision,
        "source_epoch": source_epoch,
        "digest": digest,
        "coverage": {"attributes": list(OBSERVATION_ATTRIBUTES[family])},
        "document": json.loads(canonical),
        "observed_at": observed_at,
    }
