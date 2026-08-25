# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Attempt-addressable Apply settlement against the adapter A1 evidence shape."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from ipam.models import VLAN

from ._adapter_http import make_response
from ._outbox_case import make_managed, without_commit_drain

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
        "updated_at": "2026-08-01T10:01:00Z",
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
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push

        with without_commit_drain(), suppress_intent_push():
            vlan = VLAN.objects.create(vid=vid, name=f"attempt-vlan-{vid}")
            row = NSOVLANState.objects.create(management=self.management, vlan=vlan, status="accepted")
        NSOVLANState.objects.filter(pk=row.pk).update(status="deploying", apply_attempt_id=attempt_id)
        row.refresh_from_db()
        return row

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

    def test_an_aged_settled_attempt_without_device_readback_fails_its_row(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1631, attempt_id)
        self._local_attempt(attempt_id, 71, {"vlan": 501})
        evidence = _attempt(attempt_id, self.adapter_device_id, 71, {"vlan": 501}, "settled")

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
