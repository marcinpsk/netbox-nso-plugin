# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Validate settlement response shapes before the settlement walk reads them."""

import json
import math
from importlib.resources import files
from uuid import UUID

from jsonschema import Draft202012Validator

_SCHEMA = json.loads(files("netbox_nso_plugin").joinpath("schemas/settlements.json").read_text(encoding="utf-8"))
_ARRAY_COMPONENTS = frozenset({"JobEvidenceOut", "DeviceGenerationEvidenceOut"})
_ITEM_IDENTITIES = {
    "JobEvidenceOut": "JobIdentityOut",
    "DeviceGenerationEvidenceOut": "GenerationIdentityOut",
    "DeploymentGenerationEvidenceOut": "GenerationIdentityOut",
    "DeploymentHeadEvidenceOut": "GenerationIdentityOut",
    "DeploymentAttemptEvidenceOut": "AttemptIdentityOut",
    "SettlementReceiptEvidenceOut": "ReceiptIdentityOut",
}
APPLY_SKIP_REASONS = frozenset(
    _SCHEMA["components"]["schemas"]["ActionApplyOut"]["properties"]["skipped"]["additionalProperties"]["enum"]
)
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
    for component in (*_ARRAY_COMPONENTS, "DeviceDeploymentEvidenceOut", "ActionApplyOut", "SettlementObservationOut")
}
_ITEM_VALIDATORS = {
    name: Draft202012Validator({**_SCHEMA, "$ref": f"#/components/schemas/{name}"})
    for name in (*_ITEM_IDENTITIES, *_ITEM_IDENTITIES.values())
}


class _EvidenceProjection(dict):
    """Keep the validated envelope authority for later cross-item checks."""

    def __init__(self, projection, identity, digest):
        super().__init__(projection)
        self.evidence_identity = dict(identity)
        self.evidence_digest = digest


def valid_store_incarnation(value):
    """Accept only the canonical UUID string that identifies an adapter store."""
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _jsonb_text(value):
    """Return whether PostgreSQL JSONB can store *value* as text: valid UTF-8 and no NUL."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\x00" not in value


def within_wire_bounds(body):
    """Return whether *body* nests at most ``MAX_NESTING`` deep and PostgreSQL can store it, without recursion."""
    stack = [(body, 1)]
    while stack:
        value, depth = stack.pop()
        if type(value) is int and value not in _INT64:
            return False
        if type(value) is float and not math.isfinite(value):
            return False
        if type(value) is str and not _jsonb_text(value):
            return False
        if isinstance(value, dict | list):
            if depth > MAX_NESTING:
                return False
            if isinstance(value, dict):
                stack.extend((key, depth) for key in value)
            stack.extend((item, depth + 1) for item in (value.values() if isinstance(value, dict) else value))
    return True


def validate_settlement_response(component, body, device_id):
    """Raise a closed diagnostic for malformed successful HTTP responses."""
    from .adapter_client import InvalidJobEvidence

    reason = "invalid job envelope" if component == "JobEvidenceOut" else "invalid settlement evidence"
    # Bound first: the validator and later comparisons recurse, and PostgreSQL must store every value.
    if not within_wire_bounds(body):
        raise InvalidJobEvidence({"device_id": device_id}, reason)
    if component in {"JobEvidenceOut", "DeviceGenerationEvidenceOut"}:
        if not isinstance(body, list):
            raise InvalidJobEvidence({"device_id": device_id}, reason)
        return [validate_settlement_item(component, item, device_id) for item in body]
    if component == "DeviceDeploymentEvidenceOut":
        return _deployment_evidence(body, device_id)
    errors = list(VALIDATORS[component].iter_errors(body))
    if not errors:
        return body
    raise InvalidJobEvidence({"device_id": device_id}, reason)


def validate_settlement_item(component, item, device_id):
    """Keep item failures separate after validating the independent stored identity."""
    from .adapter_client import (
        AdapterError,
        EvidenceItem,
        InvalidJobEvidence,
        _validated_generation_listing,
        _validated_job,
    )

    reason = "invalid job envelope" if component == "JobEvidenceOut" else "invalid settlement evidence"
    identity = item.get("identity") if isinstance(item, dict) else None
    if (
        not within_wire_bounds(item)
        or not _ITEM_VALIDATORS[_ITEM_IDENTITIES[component]].is_valid(identity)
        or device_id is not None
        and identity.get("device_id") != device_id
        or any(
            type(identity[key]) is not int or identity[key] <= 0
            for key in ("id", "generation_id", "seq")
            if key in identity
        )
        or any(
            identity.get(key) is not None and (type(identity[key]) is not int or identity[key] <= 0)
            for key in ("carrier_job_id", "settle_seq")
        )
    ):
        raise InvalidJobEvidence({"device_id": device_id}, reason)
    job_identity = {**identity, "id": identity.get("id", identity.get("carrier_job_id"))}
    error = InvalidJobEvidence(job_identity, reason)
    projection = item.get("projection")
    evidence_error = item.get("evidence_error")
    candidate = {**item, "generations": []} if component == "DeploymentAttemptEvidenceOut" else item
    valid = (
        _ITEM_VALIDATORS[component].is_valid(candidate)
        and (projection is None) != (evidence_error is None)
        and (evidence_error is None or evidence_error["digest"] == item["digest"])
    )
    if valid and projection is not None:
        valid = all(projection[key] == value for key, value in identity.items() if key in projection)
        try:
            if valid and component == "JobEvidenceOut":
                _validated_job(projection, "job")
            elif valid and component == "DeviceGenerationEvidenceOut":
                _validated_generation_listing(projection, device_id)
        except AdapterError:
            valid = False
    if valid and projection is not None:
        decoded = _EvidenceProjection(projection, identity, item["digest"])
    else:
        decoded = EvidenceItem(identity, error, digest=item.get("digest"))
    if component == "DeploymentAttemptEvidenceOut":
        generations = item.get("generations")
        if not isinstance(generations, list):
            decoded = EvidenceItem(identity, error, digest=item.get("digest"))
            generations = []
        decoded["generations"] = [
            validate_settlement_item("DeploymentGenerationEvidenceOut", row, device_id) for row in generations
        ]
    return decoded


def _deployment_evidence(body, device_id):
    """Validate the response frame and decode each independent evidence item."""
    from .adapter_client import InvalidJobEvidence

    if not isinstance(body, dict):
        raise InvalidJobEvidence({"device_id": device_id}, "invalid settlement evidence")
    frame = {**body}
    if isinstance(frame.get("attempts"), list):
        frame["attempts"] = []
    if isinstance(frame.get("head"), dict):
        frame["head"] = None
    if not VALIDATORS["DeviceDeploymentEvidenceOut"].is_valid(frame) or body["device_id"] != device_id:
        raise InvalidJobEvidence({"device_id": device_id}, "invalid settlement evidence")
    return {
        **body,
        "head": None
        if body["head"] is None
        else validate_settlement_item("DeploymentHeadEvidenceOut", body["head"], device_id),
        "attempts": [
            validate_settlement_item("DeploymentAttemptEvidenceOut", row, device_id) for row in body["attempts"]
        ],
    }
