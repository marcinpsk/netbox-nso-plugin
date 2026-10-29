# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Validate settlement response shapes before the settlement walk reads them."""

import json
from importlib.resources import files
from uuid import UUID

from jsonschema import Draft202012Validator

_SCHEMA = json.loads(files("netbox_nso_plugin").joinpath("schemas/settlements.json").read_text(encoding="utf-8"))
_ARRAY_COMPONENTS = frozenset({"JobOut", "DeviceGenerationOut"})
# The deepest live settlement response measured 12 levels (2026-10-29).
MAX_NESTING = 64
# Every integer that settlement persists lands in a PostgreSQL bigint column.
_INT64 = range(-(2**63), 2**63)
VALIDATORS = {
    component: Draft202012Validator(
        {
            **_SCHEMA,
            **(
                {"type": "array", "items": {"$ref": f"#/components/schemas/{component}"}}
                if component in _ARRAY_COMPONENTS
                else {"$ref": f"#/components/schemas/{component}"}
            ),
        }
    )
    for component in ("JobOut", "DeviceGenerationOut", "DeviceDeploymentEvidenceOut", "ActionApplyOut")
}


def valid_store_incarnation(value):
    """Accept only the canonical UUID string that identifies an adapter store."""
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _error_identity(component, body, path):
    """Attribute only a path inside one identified carrier element or head."""
    path = tuple(path)
    if component in _ARRAY_COMPONENTS:
        if not path or type(path[0]) is not int or not isinstance(body, list):
            return None
        row = body[path[0]]
        key = "id" if component == "JobOut" else "carrier_job_id"
        prefix = path[:1]
    elif path[:1] == ("head",):
        row, key, prefix = body.get("head"), "carrier_job_id", path[:1]
    elif (
        len(path) >= 4
        and path[0] == "attempts"
        and type(path[1]) is int
        and path[2] == "generations"
        and type(path[3]) is int
    ):
        row = body["attempts"][path[1]]["generations"][path[3]]
        key, prefix = "carrier_job_id", path[:4]
    else:
        return None
    if not isinstance(row, dict):
        return None
    identity = {"settle_seq": row.get("settle_seq")} if key == "id" else {}
    if type(row.get(key)) is int and row[key] > 0:
        identity["id"] = row[key]
    return (prefix, identity) if identity else None


def within_wire_bounds(body):
    """Return whether *body* nests at most ``MAX_NESTING`` deep and holds only int64 integers, without recursion."""
    stack = [(body, 1)]
    while stack:
        value, depth = stack.pop()
        if type(value) is int and value not in _INT64:
            return False
        if isinstance(value, dict | list):
            if depth > MAX_NESTING:
                return False
            stack.extend((item, depth + 1) for item in (value.values() if isinstance(value, dict) else value))
    return True


def validate_settlement_response(component, body, device_id):
    """Raise a closed diagnostic for malformed successful HTTP responses."""
    from .adapter_client import InvalidJobEvidence

    reason = "invalid job envelope" if component == "JobOut" else "invalid settlement evidence"
    # Bound first: the validator and later comparisons recurse, and the database stores int64.
    if not within_wire_bounds(body):
        raise InvalidJobEvidence({"device_id": device_id}, reason)
    errors = list(VALIDATORS[component].iter_errors(body))
    if not errors:
        return body
    identities = [_error_identity(component, body, error.absolute_path) for error in errors]
    job = {"device_id": device_id}
    if identities[0] is not None and all(identity == identities[0] for identity in identities):
        job.update(identities[0][1])
    raise InvalidJobEvidence(job, reason)
