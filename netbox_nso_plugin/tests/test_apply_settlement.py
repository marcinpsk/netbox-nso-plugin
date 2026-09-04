# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Attempt-addressable Apply settlement against the adapter A1 evidence shape."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone

from ._adapter_http import make_response
from ._outbox_case import make_managed, mirror_update, own_vlan

_CLIENT_CONFIG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


def _generation(generation_id, status, selected, *, result=None, error=None):
    return {
        "generation_id": generation_id,
        "seq": generation_id,
        "status": status,
        "sections": sorted(selected),
        "source_push_seq": dict(selected),
        "carrier_job_id": 900 + generation_id,
        "carrier_job_status": "failed" if status in {"failed", "outcome_unknown"} else "succeeded",
        "carrier_job_result": result,
        "carrier_job_error": error,
        "updated_at": (timezone.now() - timedelta(days=1)).isoformat(),
    }


def _response(adapter_device_id, generation_id, selected):
    return {
        "device_id": adapter_device_id,
        "outcome": "promoted",
        "job_id": 900 + generation_id,
        "selected": dict(selected),
        "skipped": {},
        "skipped_detail": None,
        "generations": [
            {
                "generation_id": generation_id,
                "seq": generation_id,
                "job_id": 900 + generation_id,
                "mode": "networked",
                "source_push_seq": dict(selected),
                "stream_revisions": {stream: 1 for stream in selected},
                "digest": f"{generation_id:064x}",
            }
        ],
    }


def _attempt(attempt_id, adapter_device_id, generation_id, selected, status, *, result=None, error=None):
    return {
        "apply_attempt_id": str(attempt_id),
        "admission_state": "admitted",
        "http_status": 202,
        "response": _response(adapter_device_id, generation_id, selected),
        "generations": [_generation(generation_id, status, selected, result=result, error=error)],
    }


def _payload(adapter_device_id, attempts, *, head=None):
    return {
        "device_id": adapter_device_id,
        "head": head,
        "blocked": bool(head and head["status"] in {"failed", "outcome_unknown"}),
        "write_work_pending": False,
        "held_jobs": [],
        "pending_generations": 0,
        "attempts": attempts,
        "unknown_apply_attempt_ids": [],
    }


class TestAttemptSettlement(TestCase):
    def setUp(self):
        self.adapter_device_id = 1626
        self.device, self.management = make_managed("attempt-settlement", self.adapter_device_id)

    def _vlan_row(self, vid, attempt_id):
        row = own_vlan(self.management, vid, f"attempt-{vid}")
        return mirror_update(row, status="deploying", apply_attempt_id=attempt_id)

    def _local_attempt(self, attempt_id, generation_id, selected, *, answered=True):
        from netbox_nso_plugin.models import NSOApplyAttempt

        response = _response(self.adapter_device_id, generation_id, selected)
        return NSOApplyAttempt.objects.create(
            id=attempt_id,
            management=self.management,
            adapter_device_id=self.adapter_device_id,
            selected=selected,
            scope_revisions={"vlan": 1},
            http_status=202 if answered else None,
            response=response if answered else None,
        )

    def test_a_blocked_attempt_fails_only_its_rows_and_not_a_waiting_successor(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        first_id, second_id = uuid4(), uuid4()
        first = self._vlan_row(1626, first_id)
        second = self._vlan_row(1627, second_id)
        self._local_attempt(first_id, 31, {"vlan": 101})
        self._local_attempt(second_id, 32, {"vlan": 102})
        first_evidence = _attempt(first_id, self.adapter_device_id, 31, {"vlan": 101}, "failed")
        second_evidence = _attempt(second_id, self.adapter_device_id, 32, {"vlan": 102}, "pending")

        settle_apply_attempts(
            self.management,
            _payload(self.adapter_device_id, [first_evidence, second_evidence], head=first_evidence["generations"][0]),
            static_route_feed_drained=True,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, "apply_failed")
        self.assertIn("generation 31", first.last_apply_error)
        self.assertEqual(second.status, "deploying")
        self.assertEqual(second.last_apply_error, "")

    def test_old_attempt_evidence_cannot_fail_a_row_repromoted_by_a_new_attempt(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        old_id, current_id = uuid4(), uuid4()
        row = self._vlan_row(1628, current_id)
        self._local_attempt(old_id, 41, {"vlan": 201})
        self._local_attempt(current_id, 42, {"vlan": 202})
        old = _attempt(old_id, self.adapter_device_id, 41, {"vlan": 201}, "failed")
        current = _attempt(current_id, self.adapter_device_id, 42, {"vlan": 202}, "pending")

        settle_apply_attempts(
            self.management,
            _payload(self.adapter_device_id, [old, current], head=old["generations"][0]),
            static_route_feed_drained=True,
        )

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")
        self.assertEqual(row.apply_attempt_id, current_id)

    def test_correlated_interface_mtu_success_settles_its_deploying_row(self):
        from dcim.models import Interface

        from netbox_nso_plugin.apply_settlement import settle_apply_attempts
        from netbox_nso_plugin.models import NSOApplyAttempt, NSOInterfaceMtuState

        attempt_id = uuid4()
        last_apply_at = timezone.now()
        interface = Interface.objects.create(device=self.device, name="Port-channel1626", type="lag")
        row = NSOInterfaceMtuState.objects.create(
            management=self.management,
            interface=interface,
            l2_mtu=9000,
            status="deploying",
            last_apply_at=last_apply_at,
            apply_attempt_id=attempt_id,
        )
        selected = {"interface_mtu": 501}
        response = _response(self.adapter_device_id, 72, selected)
        NSOApplyAttempt.objects.create(
            id=attempt_id,
            management=self.management,
            adapter_device_id=self.adapter_device_id,
            selected=selected,
            scope_revisions={"interface_mtu": 1},
            http_status=202,
            response=response,
        )
        evidence = _attempt(
            attempt_id,
            self.adapter_device_id,
            72,
            selected,
            "settled",
            result={"interface_mtu_count_by_outcome": {"in_sync": 1, "apply_failed": 0}},
        )

        settle_apply_attempts(
            self.management,
            _payload(self.adapter_device_id, [evidence]),
            static_route_feed_drained=True,
        )

        row.refresh_from_db()
        self.assertEqual(row.status, "in_sync")
        self.assertEqual(row.last_apply_at, last_apply_at)
        self.assertEqual(row.apply_attempt_id, attempt_id)

    def test_an_aggregate_scope_failure_does_not_fail_unidentified_rows(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        attempt_id, unidentified_attempt_id = uuid4(), uuid4()
        first = self._vlan_row(1633, attempt_id)
        second = self._vlan_row(1634, unidentified_attempt_id)
        self._local_attempt(attempt_id, 73, {"vlan": 503})
        result = {"vlan_count_by_outcome": {"apply_failed": 1, "in_sync": 1}}
        evidence = _attempt(
            attempt_id,
            self.adapter_device_id,
            73,
            {"vlan": 503},
            "settled",
            result=result,
        )
        evidence["generations"][0]["updated_at"] = timezone.now().isoformat()

        settle_apply_attempts(
            self.management,
            _payload(self.adapter_device_id, [evidence]),
            static_route_feed_drained=True,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, "deploying")
        self.assertEqual(second.status, "deploying")

    def test_an_aged_settled_row_uses_missing_readback_not_the_scope_counter(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts
        from netbox_nso_plugin.reconcile import _stuck_deploying_grace

        attempt_id, unidentified_attempt_id = uuid4(), uuid4()
        identified = self._vlan_row(1637, attempt_id)
        unidentified = self._vlan_row(1638, unidentified_attempt_id)
        self._local_attempt(attempt_id, 74, {"vlan": 504})
        result = {"vlan_count_by_outcome": {"in_sync": 0, "apply_failed": 1}}
        evidence = _attempt(
            attempt_id,
            self.adapter_device_id,
            74,
            {"vlan": 504},
            "settled",
            result=result,
        )
        evidence["generations"][0]["updated_at"] = (
            timezone.now() - _stuck_deploying_grace() - timedelta(seconds=1)
        ).isoformat()

        settle_apply_attempts(
            self.management,
            _payload(self.adapter_device_id, [evidence]),
            static_route_feed_drained=True,
        )

        identified.refresh_from_db()
        unidentified.refresh_from_db()
        self.assertEqual(identified.status, "apply_failed")
        self.assertIn("later device reads did not show this value", identified.last_apply_error)
        self.assertEqual(unidentified.status, "deploying")

    def test_generation_timestamps_accept_whole_and_fractional_seconds(self):
        from netbox_nso_plugin.apply_settlement import _parse_time

        for value in ("2026-08-01T10:01:00Z", "2026-08-01T10:01:00.123456Z"):
            with self.subTest(value=value):
                self.assertEqual(_parse_time(value).utcoffset().total_seconds(), 0)

    def test_generation_fixture_is_aged_relative_to_the_current_time(self):
        from netbox_nso_plugin.apply_settlement import _parse_time

        current = datetime(2026, 7, 1, tzinfo=UTC)
        with patch("netbox_nso_plugin.tests.test_apply_settlement.timezone.now", return_value=current):
            generation = _generation(1, "settled", {"vlan": 1})

        self.assertLess(_parse_time(generation["updated_at"]), current)

    def test_latest_route_policy_carrier_ignores_malformed_attempt_shapes(self):
        from netbox_nso_plugin.apply_settlement import latest_route_policy_carrier

        result = {"route_policy_count_by_outcome": {"in_sync": 1}}
        attempt_id = uuid4()
        evidence = {
            "attempts": [
                None,
                {"generations": None},
                {"generations": [None, {"seq": True, "carrier_job_id": 91, "carrier_job_result": result}]},
                {
                    "apply_attempt_id": str(attempt_id),
                    "generations": [
                        {
                            "seq": 7,
                            "carrier_job_id": 92,
                            "carrier_job_status": "succeeded",
                            "carrier_job_result": result,
                            "carrier_job_error": None,
                            "updated_at": "2026-08-01T10:01:00Z",
                        }
                    ],
                },
            ]
        }

        self.assertEqual(latest_route_policy_carrier(evidence)["id"], 92)
        self.assertIsNone(latest_route_policy_carrier({"attempts": {"invalid": "shape"}}))

    def test_unknown_evidence_replays_the_identical_request_and_normalizes_the_local_attempt(self):
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        attempt_id = uuid4()
        self._vlan_row(1630, attempt_id)
        local = self._local_attempt(attempt_id, 61, {"vlan": 401}, answered=False)
        known = _attempt(attempt_id, self.adapter_device_id, 61, {"vlan": 401}, "pending")
        requests = []
        admitted = False

        class ReplaySession:
            def request(_self, method, url, **kwargs):
                nonlocal admitted
                body = kwargs["json"]
                requests.append((method, url, body))
                if url.endswith("/deployment-evidence"):
                    payload = _payload(self.adapter_device_id, [known] if admitted else [])
                    payload["unknown_apply_attempt_ids"] = [] if admitted else [str(attempt_id)]
                    return make_response(200, payload)
                self.assertTrue(url.endswith("/actions/apply"))
                self.assertEqual(
                    body,
                    {"apply_attempt_id": str(attempt_id), "selected": {"vlan": 401}},
                )
                admitted = True
                return make_response(202, known["response"])

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=ReplaySession()),
        ):
            evidence = load_deployment_evidence(self.management)

        self.assertEqual(evidence["unknown_apply_attempt_ids"], [])
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for _method, url, _body in requests],
            ["deployment-evidence", "apply", "deployment-evidence"],
        )
        local.refresh_from_db()
        self.assertEqual(local.http_status, 202)
        self.assertEqual(local.response, known["response"])

    def test_malformed_unknown_attempt_id_is_an_adapter_error(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        attempt_id = uuid4()
        self._vlan_row(1631, attempt_id)
        self._local_attempt(attempt_id, 62, {"vlan": 402}, answered=False)
        malformed = _payload(self.adapter_device_id, [])
        malformed["unknown_apply_attempt_ids"] = [{"attempt_id": str(attempt_id)}]

        with (
            patch("netbox_nso_plugin.adapter_client.get_deployment_evidence", return_value=malformed),
            self.assertRaisesRegex(AdapterError, "invalid unknown attempt UUID") as raised,
        ):
            load_deployment_evidence(self.management)

        self.assertEqual(raised.exception.code, "invalid_response")

    def test_required_attempt_is_validated_after_its_deploying_row_reconciles(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        attempt_id = uuid4()
        self._local_attempt(attempt_id, 63, {"route_policy": 403})
        changed = _attempt(
            attempt_id,
            self.adapter_device_id,
            63,
            {"route_policy": 404},
            "settled",
            result={"route_policy_count_by_outcome": {"in_sync": 1}},
        )

        with self.assertRaisesRegex(EvidenceInvariantError, "changed the Apply selection"):
            settle_apply_attempts(
                self.management,
                _payload(self.adapter_device_id, [changed]),
                static_route_feed_drained=True,
                required_attempt_ids=(attempt_id,),
            )

    def test_a_replay_job_conflict_leaves_the_attempt_unanswered(self):
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        attempt_id = uuid4()
        self._vlan_row(1635, attempt_id)
        local = self._local_attempt(attempt_id, 63, {"vlan": 403}, answered=False)
        unknown = _payload(self.adapter_device_id, [])
        unknown["unknown_apply_attempt_ids"] = [str(attempt_id)]
        requests = []

        class ConflictSession:
            def request(_self, method, url, **kwargs):
                requests.append((method, url))
                if url.endswith("/deployment-evidence"):
                    return make_response(200, unknown)
                return make_response(
                    409,
                    {
                        "error": {
                            "code": "conflict",
                            "message": "A job is already queued or running for this device",
                            "detail": {"job_id": 900},
                        }
                    },
                )

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=ConflictSession()),
            self.assertLogs("netbox_nso_plugin.apply_settlement", level="INFO") as logs,
        ):
            load_deployment_evidence(self.management)

        self.assertEqual(
            [url.rsplit("/", 1)[-1] for _method, url in requests],
            ["deployment-evidence", "apply", "deployment-evidence"],
        )
        local.refresh_from_db()
        self.assertIsNone(local.http_status)
        self.assertIsNone(local.response)
        self.assertTrue(any("job 900" in message for message in logs.output))

    def test_unknown_generation_status_is_a_non_actionable_contract_error(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1629, attempt_id)
        self._local_attempt(attempt_id, 51, {"vlan": 301})
        evidence = _attempt(attempt_id, self.adapter_device_id, 51, {"vlan": 301}, "future_status")

        with self.assertRaises(EvidenceInvariantError):
            settle_apply_attempts(
                self.management,
                _payload(self.adapter_device_id, [evidence]),
                static_route_feed_drained=True,
            )

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_settled_generation_rejects_a_failed_carrier_snapshot(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1639, attempt_id)
        self._local_attempt(attempt_id, 53, {"vlan": 303})
        evidence = _attempt(
            attempt_id,
            self.adapter_device_id,
            53,
            {"vlan": 303},
            "settled",
            result={"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0}},
        )
        evidence["generations"][0]["carrier_job_status"] = "failed"

        with self.assertRaisesRegex(EvidenceInvariantError, "settled generation has an invalid carrier snapshot"):
            settle_apply_attempts(
                self.management,
                _payload(self.adapter_device_id, [evidence]),
                static_route_feed_drained=True,
            )

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_settled_generation_rejects_a_missing_carrier_result(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1641, attempt_id)
        self._local_attempt(attempt_id, 55, {"vlan": 305})
        evidence = _attempt(attempt_id, self.adapter_device_id, 55, {"vlan": 305}, "settled")

        with self.assertRaisesRegex(EvidenceInvariantError, "settled generation has an invalid carrier snapshot"):
            settle_apply_attempts(
                self.management,
                _payload(self.adapter_device_id, [evidence]),
                static_route_feed_drained=True,
            )

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_settled_generation_rejects_an_unknown_scope_outcome(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1640, attempt_id)
        self._local_attempt(attempt_id, 54, {"vlan": 304})
        evidence = _attempt(
            attempt_id,
            self.adapter_device_id,
            54,
            {"vlan": 304},
            "settled",
            result={"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0, "unknown": 1}},
        )

        with self.assertRaisesRegex(EvidenceInvariantError, "invalid vlan outcome counts"):
            settle_apply_attempts(
                self.management,
                _payload(self.adapter_device_id, [evidence]),
                static_route_feed_drained=True,
            )

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_a_non_list_generation_collection_is_a_contract_error(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1636, attempt_id)
        self._local_attempt(attempt_id, 52, {"vlan": 302})
        evidence = _attempt(attempt_id, self.adapter_device_id, 52, {"vlan": 302}, "running")
        evidence["generations"] = None

        with self.assertRaises(EvidenceInvariantError):
            settle_apply_attempts(
                self.management,
                _payload(self.adapter_device_id, [evidence]),
                static_route_feed_drained=True,
            )

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_an_aged_settled_attempt_without_device_readback_fails_its_row(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1631, attempt_id)
        self._local_attempt(attempt_id, 71, {"vlan": 501})
        evidence = _attempt(
            attempt_id,
            self.adapter_device_id,
            71,
            {"vlan": 501},
            "settled",
            result={"vlan_count_by_outcome": {"in_sync": 0, "apply_failed": 0}},
        )

        settle_apply_attempts(
            self.management,
            _payload(self.adapter_device_id, [evidence]),
            static_route_feed_drained=True,
        )

        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
        self.assertIn("later device reads did not show this value", row.last_apply_error)

    def test_an_abandoned_attempt_fails_immediately_with_a_truthful_reason(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1632, attempt_id)
        self._local_attempt(attempt_id, 72, {"vlan": 502})
        evidence = _attempt(attempt_id, self.adapter_device_id, 72, {"vlan": 502}, "abandoned")

        settle_apply_attempts(
            self.management,
            _payload(self.adapter_device_id, [evidence]),
            static_route_feed_drained=True,
        )

        row.refresh_from_db()
        self.assertEqual(row.status, "apply_failed")
        self.assertIn("was abandoned", row.last_apply_error)

    def test_a_deleted_overlay_is_an_absent_mirror_fragment(self):
        from django.db import transaction

        row = self._vlan_row(1637, uuid4())
        type(row).objects.filter(pk=row.pk).delete()

        from netbox_nso_plugin.intent_state import mirror_refresh
        from netbox_nso_plugin.signals import suppress_intent_push

        with transaction.atomic(), suppress_intent_push(), mirror_refresh(row, {"status"}) as locked:
            self.assertIsNone(locked)
