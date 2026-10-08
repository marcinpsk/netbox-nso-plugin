# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Validate and copy immutable family observations with the adapter schema."""

import hashlib
import json
from datetime import datetime
from importlib.resources import files

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

_SCHEMA = json.loads(files("netbox_nso_plugin").joinpath("schemas/observations.json").read_text(encoding="utf-8"))
OBSERVATION_SCHEMAS = {
    family: Draft202012Validator({**_SCHEMA, "$ref": f"#/components/schemas/{component}"})
    for family, component in _SCHEMA["families"].items()
}


class ObservationProtocolError(ValueError):
    """The observation does not satisfy the family publication contract."""


def _require(condition, reason):
    if not condition:
        raise ObservationProtocolError(f"Invalid observation: {reason}.")


def observation_defaults(family, revision, source_epoch, observation):
    """Return an independent snapshot bound to the admitted payload identity."""
    _require(family in OBSERVATION_SCHEMAS, "unsupported family")
    _require(isinstance(observation, dict), "missing document")
    _require(observation.get("family") == family, "family mismatch")
    for field, expected in (("revision", revision), ("source_epoch", source_epoch)):
        _require(
            type(expected) is int and type(observation.get(field)) is int and observation[field] == expected,
            f"{field} mismatch",
        )
    try:
        OBSERVATION_SCHEMAS[family].validate(observation)
        canonical = json.dumps(observation["document"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        observed_at = datetime.fromisoformat(observation["observed_at"])
    except (ValidationError, TypeError, ValueError) as exc:
        raise ObservationProtocolError("Invalid observation: schema or timestamp.") from exc
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    _require(observation["digest"] == digest, "digest mismatch")
    _require(observed_at.utcoffset() is not None, "observed_at needs a UTC offset")
    return {
        "revision": revision,
        "source_epoch": source_epoch,
        "digest": digest,
        "coverage": json.loads(json.dumps(observation["coverage"], allow_nan=False)),
        "document": json.loads(canonical),
        "observed_at": observed_at,
    }
