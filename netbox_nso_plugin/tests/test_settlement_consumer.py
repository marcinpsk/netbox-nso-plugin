# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""The feed cursor consumes certified executions without writing overlay lifecycle."""

from __future__ import annotations

from copy import deepcopy

from ._settlement_case import (
    _execution_result,
    _make_device,
    _make_mgmt,
    _own,
    _record_execution,
    _result,
    _route,
    _SettlementCase,
    _stale_clock,
)


class TestExecutionConsumption(_SettlementCase):
    def _waiting_route(self, tag):
        device = _make_device(tag)
        management = _make_mgmt(device, tag, 70, adapter=self.adapter)
        route = _route("198.18.0.0/24", "198.18.0.1", devices=[device])
        state = _own(route, management, generation=51)
        return management, route, state

    def test_legacy_route_success_cannot_replace_a_structured_execution(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        management, route, state = self._waiting_route("legacy-success")
        self.adapter.store.terminal_job(70, results=[_result(route.pk, 51)])
        _stale_clock(state)

        result = consume_static_route_settlements(management)

        state.refresh_from_db()
        assert state.status == "deploying"
        assert state.last_apply_at is None
        assert state.last_apply_error == ""
        assert result.stalled
        assert self._cursor(management).settle_cursor_seq == 0
        assert self.adapter.store.readback_requests == []

    def test_legacy_route_failure_cannot_change_overlay_lifecycle(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        management, route, state = self._waiting_route("legacy-failure")
        self.adapter.store.terminal_job(70, results=[_result(route.pk, 51, outcome="apply_failed")], status="failed")

        result = consume_static_route_settlements(management)

        state.refresh_from_db()
        assert state.status == "deploying"
        assert state.last_apply_error == ""
        assert result.stalled
        assert result.consumed == 0

    def test_sparse_removal_evidence_stays_unresolved(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        management, _route_row, state = self._waiting_route("sparse-removal")
        self.adapter.store.terminal_job(70, job_type="removal")

        result = consume_static_route_settlements(management)

        state.refresh_from_db()
        assert state.status == "deploying"
        assert result.stalled
        assert result.cursor == 0

    def test_a_consumed_execution_advances_without_rewriting_overlay_state(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        management, _route_row, state = self._waiting_route("consumed")
        prior = (state.status, state.last_apply_at, state.last_apply_error, state.apply_attempt_id)
        job = self.adapter.store.terminal_job(70, extra=_execution_result())
        _record_execution(management, self.adapter.store, job)
        self.adapter.store.requests.clear()

        result = consume_static_route_settlements(management)

        state.refresh_from_db()
        assert (state.status, state.last_apply_at, state.last_apply_error, state.apply_attempt_id) == prior
        assert result.consumed == 1
        assert result.cursor == job["settle_seq"]
        assert not result.stalled
        assert not any(method == "PUT" for method, _path in self.adapter.store.requests)

    def test_an_execution_from_another_store_cannot_release_the_cursor(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        management, _route_row, _state = self._waiting_route("wrong-store")
        job = self.adapter.store.terminal_job(70, extra=_execution_result())
        execution = _record_execution(management, self.adapter.store, job)
        execution.store_incarnation = "another-store"
        execution.save(update_fields=("store_incarnation",))

        result = consume_static_route_settlements(management)

        assert result.stalled
        assert result.cursor == 0

    def test_an_unconsumed_execution_cannot_release_the_cursor(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        management, _route_row, _state = self._waiting_route("unconsumed")
        job = self.adapter.store.terminal_job(70, extra=_execution_result())
        _record_execution(management, self.adapter.store, job, state="unconsumed")

        result = consume_static_route_settlements(management)

        assert result.stalled
        assert result.cursor == 0

    def test_a_changed_carrier_certificate_cannot_release_the_cursor(self):
        from netbox_nso_plugin.settlement import consume_static_route_settlements

        management, _route_row, _state = self._waiting_route("changed-result")
        job = self.adapter.store.terminal_job(70, extra=_execution_result())
        execution = _record_execution(management, self.adapter.store, job)
        projection = deepcopy(execution.terminal_projection)
        projection["carrier_job_result"]["commit_acknowledged"] = False
        execution.terminal_projection = projection
        execution.save(update_fields=("terminal_projection",))

        result = consume_static_route_settlements(management)

        assert result.stalled
        assert result.cursor == 0
