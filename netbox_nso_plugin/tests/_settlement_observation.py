# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Serve the adapter settlement observation from a test double's own feed, listing and evidence.

The shape follows SettlementObservationOut in the vendored settlement schema. As in the adapter,
a generation whose carrier job holds invalid evidence is an identified error item, because the
observation checks the complete carrier result.
"""

import copy
import hashlib
import json

from ._adapter_http import make_response
from ._settlement_wire import job_wire

INCARNATION = "00000000-0000-4000-8000-000000000004"


def failed_item(item, code="execution_contract"):
    """Keep the trusted identity and digest; drop the projection."""
    return {**item, "projection": None, "evidence_error": {"code": code, "digest": item["digest"]}}


STRUCTURED_RESULT_KEYS = frozenset({"row_outcomes", "refusals", "settlement_owners", "commit_acknowledged"})


def published_job(job):
    """Publish a feed job as the adapter does: a terminal Apply without structured evidence is an error item."""
    result = job["result"]
    structured = isinstance(result, dict) and result.keys() >= STRUCTURED_RESULT_KEYS
    if job["type"] in {"apply", "removal"} and job["status"] in {"succeeded", "failed"} and not structured:
        return failed_item(job_wire(job))
    return job_wire(job)


def observation_document(original, url, body, device_id, incarnation):
    base = url.rsplit("/settlement-observation", 1)[0]
    root = base.split("/api/v1/", 1)[0]
    job_id, after = body["job_id"], body["after_settle_seq"]
    requested = sorted(body["apply_attempt_ids"])
    feed = original(
        "GET",
        f"{root}/api/v1/jobs",
        params={"device_id": str(device_id), "order": "asc", "after_settle_seq": "0", "limit": "1000"},
    ).json()
    job = next((item for item in feed if item["identity"]["id"] == job_id), None)
    ordered = sorted(
        (item["identity"] for item in feed if item["identity"]["settle_seq"] is not None),
        key=lambda identity: identity["settle_seq"],
    )
    next_feed = next((identity for identity in ordered if identity["settle_seq"] > after), None)
    listing = original("GET", f"{base}/generations", params={"limit": "500"}).json()
    deployment = original("POST", f"{base}/deployment-evidence", json={"apply_attempt_ids": requested}).json()
    head = deployment["head"]
    head_id = head["identity"]["generation_id"] if head is not None else None
    generations = [
        row
        for row in listing
        if row["identity"]["carrier_job_id"] == job_id
        or (row["projection"] or {}).get("job_id") == job_id
        or row["identity"]["apply_attempt_id"] in requested
        or row["identity"]["generation_id"] == head_id
    ]
    if job is not None and job["projection"] is None:

        def checked(item):
            return failed_item(item) if item["identity"]["carrier_job_id"] == job_id else item

        generations = [checked(row) for row in generations]
        if head is not None:
            deployment["head"] = checked(head)
        for attempt in deployment["attempts"]:
            attempt["generations"] = [checked(row) for row in attempt["generations"]]
    document = {
        "request": {"job_id": job_id, "after_settle_seq": after, "apply_attempt_ids": requested},
        "store_incarnation": incarnation,
        "device": {
            "device_id": device_id,
            "netbox_device_id": None,
            "nso_instance": "placeholder-instance",
            "nso_device_name": "placeholder-device",
        },
        "job": copy.deepcopy(job),
        "next_feed": next_feed,
        "generations": generations,
        "deployment": deployment,
        "receipts": [],
        "absent": {
            "job_id": None if job is not None else job_id,
            "apply_attempt_ids": sorted(deployment["unknown_apply_attempt_ids"]),
            "receipt_sections": [],
        },
    }
    document["digest"] = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    return document


def serve_observation(case, *, incarnation=INCARNATION, edit=None):
    """Answer POST settlement-observation; *edit* may change the document before it is sent."""
    original = case.adapter._handle
    case.observation_requests = []

    def handle(method, url, **kwargs):
        if method == "POST" and url.endswith("/settlement-observation"):
            body = kwargs["json"]
            case.observation_requests.append(body)
            document = observation_document(original, url, body, case.management.adapter_device_id, incarnation)
            if edit is not None:
                edit(document)
            return make_response(200, document)
        return original(method, url, **kwargs)

    case.adapter._handle = handle
