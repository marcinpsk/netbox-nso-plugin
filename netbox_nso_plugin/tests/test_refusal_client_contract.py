# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Read the shipped preview and job envelopes through real HTTP response decoding."""

import json
from unittest.mock import patch

import pytest

from netbox_nso_plugin import adapter_client

from ._adapter_http import make_response, make_transport_session
from .test_refusal_result import refused_bgp_result

CONFIG = {
    "url": "http://adapter.invalid",
    "token": "placeholder",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 5,
}


def _response(payload):
    return (
        patch("netbox_nso_plugin.adapter_client._resolve_config", autospec=True, return_value=CONFIG),
        patch(
            "netbox_nso_plugin.adapter_client.requests.Session",
            autospec=True,
            return_value=make_transport_session(make_response(json_data=payload)),
        ),
    )


@pytest.mark.parametrize("status", ["ready", "refused", "unavailable", "error"])
def test_preview_accepts_the_exact_structured_adapter_shape(status):
    preview = {
        "device_id": 7,
        "outformat": "cli",
        "diffs": {} if status == "ready" else {"device_intent": "! preview unavailable"},
        "generation_id": 17,
        "document_digest": "a" * 64,
        "status": status,
        "refusals": refused_bgp_result()["refusals"] if status == "refused" else [],
    }
    config, transport = _response(preview)
    with config, transport:
        assert adapter_client.get_apply_diff(7, "cli") == preview


def test_job_rejects_typed_projection_drift_before_it_reaches_a_view():
    result = refused_bgp_result()
    job = {"id": 23, "type": "apply", "status": "failed", "result": result, "row_outcomes": [], "refusals": []}
    config, transport = _response(job)
    with config, transport, pytest.raises(adapter_client.AdapterError, match="execution evidence"):
        adapter_client.get_job(23)


def test_job_rejects_the_old_two_count_keys():
    result = refused_bgp_result()
    result["bgp_count_by_outcome"] = {"in_sync": 0, "apply_failed": 1}
    job = {
        "id": 23,
        "type": "apply",
        "status": "failed",
        "result": result,
        "row_outcomes": result["row_outcomes"],
        "refusals": result["refusals"],
    }
    config, transport = _response(job)
    with config, transport, pytest.raises(adapter_client.AdapterError, match="execution evidence"):
        adapter_client.get_job(23)


def test_job_status_projects_all_four_validated_counts(rf):
    from netbox_nso_plugin.views import NSOJobStatusView

    result = refused_bgp_result()
    job = {
        "id": 23,
        "type": "apply",
        "status": "failed",
        "result": result,
        "row_outcomes": result["row_outcomes"],
        "refusals": result["refusals"],
    }
    config, transport = _response(job)
    with config, transport:
        response = NSOJobStatusView().get(rf.get("/"), 23)
    assert response.status_code == 200
    assert json.loads(response.content)["outcome_counts"] == [
        {
            "scope": "bgp",
            "in_sync": 0,
            "apply_failed": 1,
            "not_applied": 0,
            "unavailable": 0,
        }
    ]
