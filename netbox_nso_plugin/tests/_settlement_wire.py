# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Serialize plain settlement fixtures at the HTTP test boundary."""

from ._adapter_http import make_evidence_envelope


def job_wire(job):
    return make_evidence_envelope(job, identity={key: job.get(key) for key in ("id", "device_id", "settle_seq")})


def generation_wire(generation, device_id):
    return make_evidence_envelope(
        generation,
        identity={
            "device_id": device_id,
            **{key: generation.get(key) for key in ("generation_id", "seq", "carrier_job_id", "apply_attempt_id")},
        },
    )


def deployment_wire(evidence):
    device_id = evidence["device_id"]
    attempts = []
    for attempt in evidence["attempts"]:
        projection = {key: value for key, value in attempt.items() if key != "generations"}
        projection["selected"] = attempt["response"].get("selected", {})
        projection["generation_ids"] = [row["generation_id"] for row in attempt["response"].get("generations", [])]
        generations = attempt["generations"]
        attempts.append(
            make_evidence_envelope(
                projection,
                identity={"device_id": device_id, "apply_attempt_id": attempt["apply_attempt_id"]},
                generations=[generation_wire(row, device_id) for row in generations]
                if isinstance(generations, list)
                else generations,
            )
        )
    return {
        **evidence,
        "head": generation_wire(evidence["head"], device_id) if evidence["head"] is not None else None,
        "attempts": attempts,
    }
