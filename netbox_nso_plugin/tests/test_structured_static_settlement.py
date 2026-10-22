# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Static-route proof supplements the authoritative logical row disposition."""

from types import SimpleNamespace

import pytest

from ._settlement_case import FINGERPRINT, _make_device, _make_mgmt, _own, _result, _route, _SettlementCase


class TestStructuredStaticResults(_SettlementCase):
    def test_static_not_applied_overrides_legacy_failure_projection(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        device = _make_device("structured-static")
        management = _make_mgmt(device, "structured-static", 77)
        self.adapter.store.add_device(nso_instance="placeholder", nso_device_name="placeholder", device_id=77)
        route = _route("198.18.1.0/24", "198.18.0.1", devices=[device])
        state = _own(route, management, generation=51)
        carrier = {
            "stream": "static_route",
            "row": {"subject": "static-route.route", "key": "a" * 64},
            "source_push_seq": 41,
            "frozen_revision": 7,
            "accepted_at": None,
            "generation_id": 17,
            "settlement_cohort": 9,
        }
        logical = {key: value for key, value in carrier.items() if key != "accepted_at"}
        logical.update(acceptance_identity=None, carrier=carrier, disposition="not_applied", refusals=[])
        self.adapter.store.terminal_job(
            77,
            status="failed",
            results=[_result(route.pk, 51, outcome="apply_failed")],
            extra={
                "row_outcomes": [logical],
                "refusals": [],
                "settlement_owners": [{**carrier, "disposition": "not_applied"}],
                "commit_acknowledged": False,
                "static_route_count_by_outcome": {"in_sync": 0, "apply_failed": 0, "not_applied": 1, "unavailable": 0},
            },
        )

        result = consume_static_route_settlements(management)

        state.refresh_from_db()
        assert state.status == "deploying"
        assert state.last_apply_error == ""
        assert result.stalled
        assert self._cursor(management).settle_cursor_seq is None


@pytest.mark.parametrize(
    "fingerprint,route_generation,outcome,certified",
    [
        (FINGERPRINT, 51, "in_sync", True),
        ("different", 51, "in_sync", False),
        (FINGERPRINT, 52, "in_sync", False),
        (FINGERPRINT, 51, "apply_failed", False),
        (FINGERPRINT, 51, "unproven", False),
    ],
)
def test_static_success_requires_generation_and_fingerprint(fingerprint, route_generation, outcome, certified):
    from netbox_nso_plugin.settlement import static_route_success_proven

    snapshot = SimpleNamespace(
        static_route_fences={
            "route_id": 31,
            "intent_generation": 51,
            "expected_generation": 51,
            "expected_fingerprint": FINGERPRINT,
        }
    )
    generation = {
        "carrier_job_result": {
            "static_route_results": [
                _result(31, route_generation, outcome=outcome, fingerprint=fingerprint),
            ]
        }
    }
    assert static_route_success_proven(snapshot, generation) is certified
