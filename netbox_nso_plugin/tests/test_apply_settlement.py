# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Attempt-addressable Apply settlement against the adapter A1 evidence shape."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from ._adapter_http import make_generation_listing, make_response
from ._outbox_case import make_managed, mirror_update, own_vlan
from ._ownership_case import acquire_overlay
from ._settlement_wire import deployment_wire, generation_wire

_CLIENT_CONFIG = {
    "url": "http://adapter.local",
    "token": "test-token",
    "verify_tls": True,
    "ca_cert_path": None,
    "timeout": 30,
}


def _generation(generation_id, status, selected, *, result=None, error=None):
    from netbox_nso_plugin import delivery
    from netbox_nso_plugin.refusal_registry import document_section_for_stream

    return {
        "generation_id": generation_id,
        "seq": generation_id,
        "status": status,
        "sections": sorted({document_section_for_stream(stream) for stream in selected}),
        "stream_revisions": dict(selected),
        "source_push_seq": {
            stream: None if stream in delivery.direct_streams() else revision for stream, revision in selected.items()
        },
        "carrier_job_id": 900 + generation_id,
        "carrier_job_status": "queued"
        if status == "pending"
        else "running"
        if status == "running"
        else "failed"
        if status in {"failed", "outcome_unknown"}
        else "succeeded",
        "carrier_job_result": result,
        "row_outcomes": None if result is None else result.get("row_outcomes", []),
        "refusals": None if result is None else result.get("refusals", []),
        "carrier_job_error": error,
        "updated_at": (timezone.now() - timedelta(days=1)).isoformat(),
    }


def _response(adapter_device_id, generation_id, selected):
    from netbox_nso_plugin import delivery

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
                "source_push_seq": {
                    stream: None if stream in delivery.direct_streams() else revision
                    for stream, revision in selected.items()
                },
                "stream_revisions": dict(selected),
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


class _Evidence(dict):
    store_incarnation = "00000000-0000-4000-8000-000000000004"


def _payload(adapter_device_id, attempts, *, head=None):
    return _Evidence(
        {
            "device_id": adapter_device_id,
            "head": head,
            "blocked": bool(head and head["status"] in {"failed", "outcome_unknown"}),
            "write_work_pending": False,
            "held_jobs": [],
            "pending_generations": 0,
            "attempts": attempts,
            "unknown_apply_attempt_ids": [],
        }
    )


def _epoch_session(session, *, generations=()):
    """Serve epoch and generation HTTP reads around the existing request double."""

    class Session:
        def request(self, method, url, **kwargs):
            if method == "GET" and url.endswith(("/jobs", "/generations")):
                response = make_response(
                    200,
                    [generation_wire(row, int(url.split("/devices/")[1].split("/")[0])) for row in generations]
                    if url.endswith("/generations")
                    else [],
                )
                response.headers["X-Store-Incarnation"] = "00000000-0000-4000-8000-000000000004"
                return response
            return session.request(method, url, **kwargs)

    return Session()


def test_evidence_fixture_generations_match_the_adapter_evidence_field_set():
    from netbox_nso_plugin.apply_settlement import _GENERATION_FIELDS

    from ._settlement_case import _evidence_generation

    selected = {"lag": 7, "switchport": 8, "vlan": 9}
    for status in ("pending", "running", "settled", "failed", "outcome_unknown", "abandoned"):
        generation = _generation(73, status, selected, result={})
        assert set(generation) == _GENERATION_FIELDS
        assert set(_attempt(uuid4(), 1626, 73, selected, status, result={})["generations"][0]) == _GENERATION_FIELDS
    for settled_scopes in ((), ("lag", "switchport")):
        assert set(_evidence_generation(73, selected, settled_scopes)) == _GENERATION_FIELDS


_ADMISSION_WITHOUT_VIEWS = """
import sys
import django
django.setup()
from django.conf import settings
assert settings.SETTINGS_MODULE == sys.argv[1]
from netbox_nso_plugin import delivery
from netbox_nso_plugin.apply_settlement import _admission_keep_streams

class Attempt:
    selected = {"vlan": 4, "lag": 5}

source = {s: None if s in delivery.direct_streams() else r for s, r in Attempt.selected.items()}
link = {"generation_id": 1, "seq": 1, "job_id": None, "mode": "networked",
        "source_push_seq": source, "stream_revisions": dict(Attempt.selected), "digest": "d"}
promoted = {"selected": dict(Attempt.selected), "skipped": {}, "outcome": "promoted", "generations": [link]}
no_op = {"selected": dict(Attempt.selected), "skipped": {"vlan": "superseded", "lag": "superseded"},
         "outcome": "no_op", "generations": []}
assert _admission_keep_streams(Attempt(), promoted) is None
assert _admission_keep_streams(Attempt(), no_op) == ()
print("views-loaded" if "netbox_nso_plugin.views" in sys.modules else "views-absent")
"""


@pytest.mark.parametrize("settings_module", ["netbox.settings", "admission_test_settings"])
def test_admission_validation_runs_without_the_view_module(settings_module, tmp_path):
    # A fresh process: the test session has already imported views through the URLconf.
    (tmp_path / "admission_test_settings.py").write_text("from netbox.settings import *\n", encoding="utf-8")
    with override_settings(SETTINGS_MODULE=settings_module):
        env = dict(
            os.environ,
            DJANGO_SETTINGS_MODULE=settings.SETTINGS_MODULE,
            PYTHONPATH=os.pathsep.join((str(tmp_path), *filter(None, sys.path))),
        )
        child = subprocess.run(  # noqa: S603 (a fixed argv, no shell)
            [sys.executable, "-c", _ADMISSION_WITHOUT_VIEWS, settings_module],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    assert child.returncode == 0, child.stderr
    assert child.stdout.strip().splitlines()[-1] == "views-absent"


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
            adapter_apply_attempt_id=attempt_id,
            management=self.management,
            adapter_device_id=self.adapter_device_id,
            selected=selected,
            scope_revisions={"vlan": 1},
            http_status=202 if answered else None,
            response=response if answered else None,
        )

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

    def test_unknown_evidence_replays_the_identical_request_and_normalizes_the_local_attempt(self):
        import json

        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        attempt_id = uuid4()
        self._vlan_row(1630, attempt_id)
        selected = {"vlan": 401, "lag": 7, "switchport": 9}
        local = self._local_attempt(attempt_id, 61, selected, answered=False)
        local.refresh_from_db(fields=["selected"])
        known = _attempt(attempt_id, self.adapter_device_id, 61, selected, "pending")
        persisted_wire = json.dumps({"apply_attempt_id": str(attempt_id), "selected": local.selected})
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
                    return make_response(200, deployment_wire(payload))
                self.assertTrue(url.endswith("/actions/apply"))
                self.assertEqual(
                    body,
                    {"apply_attempt_id": str(attempt_id), "selected": selected},
                )
                self.assertEqual(json.dumps(body), persisted_wire)
                admitted = True
                return make_response(202, known["response"])

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch(
                "netbox_nso_plugin.adapter_client._get_session",
                return_value=_epoch_session(
                    ReplaySession(),
                    generations=[
                        make_generation_listing(
                            **known["response"]["generations"][0],
                            status="pending",
                            carrier_job_id=None,
                            carrier_job_status=None,
                        )
                    ],
                ),
            ),
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

    def test_malformed_unknown_attempt_id_is_invalid_evidence(self):
        from netbox_nso_plugin.adapter_client import InvalidJobEvidence
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        attempt_id = uuid4()
        self._vlan_row(1631, attempt_id)
        self._local_attempt(attempt_id, 62, {"vlan": 402}, answered=False)
        malformed = _payload(self.adapter_device_id, [])
        malformed["unknown_apply_attempt_ids"] = [{"attempt_id": str(attempt_id)}]

        class EvidenceSession:
            def request(_self, method, url, **kwargs):
                return make_response(200, deployment_wire(malformed))

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(EvidenceSession())),
            self.assertRaises(InvalidJobEvidence) as raised,
        ):
            load_deployment_evidence(self.management)

        self.assertEqual(raised.exception.reason, "invalid settlement evidence")

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
                    return make_response(200, deployment_wire(unknown))
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
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(ConflictSession())),
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
        self.assertTrue(any("device 1626" in message for message in logs.output))

    def test_a_replay_refusal_with_malformed_body_is_not_retried(self):
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        for payload in (None, [], "rejected"):
            with self.subTest(payload=payload):
                attempt_id = uuid4()
                local = self._local_attempt(attempt_id, 66, {"vlan": 406}, answered=False)
                unknown = _payload(self.adapter_device_id, [])
                unknown["unknown_apply_attempt_ids"] = [str(attempt_id)]
                calls = []

                class RefusingSession:
                    def request(_self, method, url, **kwargs):
                        calls.append(url.rsplit("/", 1)[-1])
                        if url.endswith("/deployment-evidence"):
                            return make_response(200, deployment_wire(unknown))
                        self.assertTrue(url.endswith("/actions/apply"))
                        return make_response(400, payload)

                with (
                    patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
                    patch(
                        "netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(RefusingSession())
                    ),
                ):
                    load_deployment_evidence(self.management)
                    from netbox_nso_plugin.apply_settlement import deployment_evidence_attempt_ids

                    self.assertNotIn(attempt_id, deployment_evidence_attempt_ids(self.management))

                local.refresh_from_db()
                self.assertEqual(local.http_status, 400)
                self.assertIsNotNone(local.response)
                self.assertEqual(calls, ["deployment-evidence", "apply", "deployment-evidence"])

    def test_a_rejected_replay_stays_terminal_when_an_overlay_references_it(self):
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        attempt_id = uuid4()
        self._vlan_row(1646, attempt_id)
        local = self._local_attempt(attempt_id, 66, {"vlan": 406}, answered=False)
        unknown = _payload(self.adapter_device_id, [])
        unknown["unknown_apply_attempt_ids"] = [str(attempt_id)]
        calls = []

        class RefusingSession:
            def request(_self, method, url, **kwargs):
                calls.append(url.rsplit("/", 1)[-1])
                if url.endswith("/deployment-evidence"):
                    return make_response(
                        200,
                        deployment_wire({**unknown, "unknown_apply_attempt_ids": kwargs["json"]["apply_attempt_ids"]}),
                    )
                self.assertTrue(url.endswith("/actions/apply"))
                return make_response(400, content=b"invalid JSON")

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(RefusingSession())),
        ):
            load_deployment_evidence(self.management)
            load_deployment_evidence(self.management)
            load_deployment_evidence(self.management, attempt_ids=(attempt_id,))

        local.refresh_from_db()
        self.assertEqual(local.http_status, 400)
        self.assertEqual(
            calls,
            ["deployment-evidence", "apply", "deployment-evidence", "deployment-evidence", "deployment-evidence"],
        )

    def test_rowless_lost_responses_expire_from_evidence_replay(self):
        from netbox_nso_plugin.apply_settlement import (
            LOST_RESPONSE_REPLAY_WINDOW,
            deployment_evidence_attempt_ids,
        )
        from netbox_nso_plugin.models import NSOApplyAttempt

        expired_id, recent_id = uuid4(), uuid4()
        expired = self._local_attempt(expired_id, 66, {"vlan": 406}, answered=False)
        self._local_attempt(recent_id, 67, {"vlan": 407}, answered=False)
        # Age the attempt one day past the replay window so the exclusion holds if the window changes.
        NSOApplyAttempt.objects.filter(pk=expired.pk).update(
            created_at=timezone.now() - LOST_RESPONSE_REPLAY_WINDOW - timedelta(days=1)
        )

        attempt_ids = deployment_evidence_attempt_ids(self.management)

        self.assertNotIn(expired_id, attempt_ids)
        self.assertIn(recent_id, attempt_ids)

    def test_an_ambiguous_replay_failure_leaves_the_attempt_answerable(self):
        """A 503 replay may still have enqueued the Apply, so it may not become the stored answer.

        The stored response is immutable and settlement rejects later evidence that differs from
        it, so recording an ambiguous failure would block that attempt's rows forever.
        """
        from dcim.models import Interface

        from netbox_nso_plugin.apply_settlement import load_deployment_evidence
        from netbox_nso_plugin.models import NSOApplyAttempt, NSOInterfaceMtuState

        attempt_id = uuid4()
        selected = {"interface_mtu": 505}
        interface = Interface.objects.create(device=self.device, name="Port-channel1642", type="lag", mtu=9000)
        row = acquire_overlay(
            NSOInterfaceMtuState,
            management=self.management,
            interface=interface,
            l2_mtu=9000,
            status="deploying",
            last_apply_at=timezone.now(),
            apply_attempt_id=attempt_id,
        )
        local = NSOApplyAttempt.objects.create(
            id=attempt_id,
            adapter_apply_attempt_id=attempt_id,
            management=self.management,
            adapter_device_id=self.adapter_device_id,
            selected=selected,
            scope_revisions={"interface_mtu": 1},
        )
        unknown = _payload(self.adapter_device_id, [])
        unknown["unknown_apply_attempt_ids"] = [str(attempt_id)]
        admitted = _attempt(
            attempt_id,
            self.adapter_device_id,
            76,
            selected,
            "pending",
        )
        applies = []
        answered = False

        class FlappingSession:
            def request(_self, method, url, **kwargs):
                nonlocal answered
                if url.endswith("/deployment-evidence"):
                    known = _payload(self.adapter_device_id, [admitted]) if answered else unknown
                    return make_response(200, deployment_wire(known))
                applies.append(kwargs["json"])
                if len(applies) == 1:
                    # Copied from ../nso-adapter/tests/api/openapi_snapshot.json ErrorEnvelope,
                    # with the nso_unreachable code the adapter answers 503 with.
                    return make_response(
                        503,
                        {"error": {"code": "nso_unreachable", "message": "NSO is not reachable", "detail": {}}},
                    )
                answered = True
                return make_response(202, admitted["response"])

        session = FlappingSession()

        def load():
            with (
                patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
                patch(
                    "netbox_nso_plugin.adapter_client._get_session",
                    return_value=_epoch_session(
                        session,
                        generations=[
                            make_generation_listing(
                                **admitted["response"]["generations"][0],
                                status="pending",
                                carrier_job_id=None,
                                carrier_job_status=None,
                            )
                        ],
                    ),
                ),
            ):
                return load_deployment_evidence(self.management)

        ambiguous = load()

        self.assertEqual(ambiguous["unknown_apply_attempt_ids"], [str(attempt_id)])
        local.refresh_from_db()
        self.assertIsNone(local.http_status)
        self.assertIsNone(local.response)

        evidence = load()

        local.refresh_from_db()
        self.assertEqual(local.http_status, 202)
        self.assertEqual(local.response, admitted["response"])
        self.assertEqual(applies, [{"apply_attempt_id": str(attempt_id), "selected": selected}] * 2)

        self.assertEqual(evidence["attempts"][0]["generations"][0]["status"], "pending")
        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_an_unrequested_unknown_attempt_is_not_replayed(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence

        requested_id = uuid4()
        extra_id = uuid4()
        self._vlan_row(1635, requested_id)
        self._local_attempt(requested_id, 64, {"vlan": 404})
        self._local_attempt(extra_id, 65, {"vlan": 405})
        evidence = _payload(self.adapter_device_id, [])
        evidence["unknown_apply_attempt_ids"] = [str(extra_id)]

        calls = []

        class EvidenceSession:
            def request(_self, method, url, **kwargs):
                calls.append(url)
                return make_response(200, deployment_wire(evidence))

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(EvidenceSession())),
            self.assertRaisesRegex(EvidenceInvariantError, "unrequested_unknown_apply_attempt"),
        ):
            load_deployment_evidence(self.management)

        self.assertFalse(any(url.endswith("/actions/apply") for url in calls))

    def test_an_unrequested_known_attempt_in_the_initial_evidence_is_rejected(self):
        from netbox_nso_plugin.apply_settlement import (
            LOST_RESPONSE_REPLAY_WINDOW,
            EvidenceInvariantError,
            settle_device_apply_attempts,
        )
        from netbox_nso_plugin.models import NSOApplyAttempt

        requested_id, extra_id = uuid4(), uuid4()
        self._vlan_row(1650, requested_id)
        self._local_attempt(requested_id, 80, {"vlan": 420})
        extra = self._local_attempt(extra_id, 81, {"vlan": 421}, answered=False)
        # Age the rowless attempt past the replay window so it leaves the requested set.
        NSOApplyAttempt.objects.filter(pk=extra.pk).update(
            created_at=timezone.now() - LOST_RESPONSE_REPLAY_WINDOW - timedelta(days=1)
        )
        payload = _payload(
            self.adapter_device_id,
            [
                _attempt(requested_id, self.adapter_device_id, 80, {"vlan": 420}, "pending"),
                _attempt(extra_id, self.adapter_device_id, 81, {"vlan": 421}, "pending"),
            ],
        )

        class EvidenceSession:
            def request(_self, method, url, **kwargs):
                self.assertTrue(url.endswith("/deployment-evidence"))
                return make_response(200, deployment_wire(payload))

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(EvidenceSession())),
            self.assertRaisesRegex(EvidenceInvariantError, "unrequested_apply_attempt"),
        ):
            settle_device_apply_attempts(self.management)

        extra.refresh_from_db()
        self.assertIsNone(extra.http_status)
        self.assertIsNone(extra.response)

    def test_an_unrequested_known_attempt_in_the_replay_refetch_is_rejected(self):
        from netbox_nso_plugin.apply_settlement import (
            LOST_RESPONSE_REPLAY_WINDOW,
            EvidenceInvariantError,
            settle_device_apply_attempts,
        )
        from netbox_nso_plugin.models import NSOApplyAttempt

        requested_id, extra_id = uuid4(), uuid4()
        self._vlan_row(1651, requested_id)
        self._local_attempt(requested_id, 82, {"vlan": 422}, answered=False)
        extra = self._local_attempt(extra_id, 83, {"vlan": 423}, answered=False)
        # Age the rowless attempt past the replay window so it leaves the requested set.
        NSOApplyAttempt.objects.filter(pk=extra.pk).update(
            created_at=timezone.now() - LOST_RESPONSE_REPLAY_WINDOW - timedelta(days=1)
        )
        requested = _attempt(requested_id, self.adapter_device_id, 82, {"vlan": 422}, "pending")
        unrequested = _attempt(extra_id, self.adapter_device_id, 83, {"vlan": 423}, "pending")
        admitted = False

        class ReplaySession:
            def request(_self, method, url, **kwargs):
                nonlocal admitted
                if url.endswith("/deployment-evidence"):
                    answered = _payload(
                        self.adapter_device_id,
                        [requested, unrequested] if admitted else [],
                    )
                    answered["unknown_apply_attempt_ids"] = [] if admitted else [str(requested_id)]
                    return make_response(200, deployment_wire(answered))
                self.assertTrue(url.endswith("/actions/apply"))
                admitted = True
                return make_response(202, requested["response"])

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(ReplaySession())),
            self.assertRaisesRegex(EvidenceInvariantError, "unrequested_apply_attempt"),
        ):
            settle_device_apply_attempts(self.management)

        extra.refresh_from_db()
        self.assertIsNone(extra.http_status)
        self.assertIsNone(extra.response)

    def test_a_malformed_attempt_record_is_rejected_before_any_replay(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence

        attempt_id = uuid4()
        self._vlan_row(1652, attempt_id)
        local = self._local_attempt(attempt_id, 86, {"vlan": 426}, answered=False)
        malformed = _attempt(attempt_id, self.adapter_device_id, 86, {"vlan": 426}, "pending")
        malformed["apply_attempt_id"] = "not-a-uuid"
        payload = _payload(self.adapter_device_id, [malformed])
        payload["unknown_apply_attempt_ids"] = [str(attempt_id)]
        requests = []

        class MalformedSession:
            def request(_self, method, url, **kwargs):
                requests.append(url.rsplit("/", 1)[-1])
                if url.endswith("/deployment-evidence"):
                    return make_response(200, deployment_wire(payload))
                return make_response(202, malformed["response"])

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(MalformedSession())),
            self.assertRaisesRegex(EvidenceInvariantError, "invalid_apply_attempt_uuid"),
        ):
            load_deployment_evidence(self.management)

        self.assertEqual(requests, ["deployment-evidence"])
        local.refresh_from_db()
        self.assertIsNone(local.http_status)
        self.assertIsNone(local.response)

    def test_a_malformed_attempt_collection_in_the_replay_refetch_is_rejected(self):
        from netbox_nso_plugin.adapter_client import AdapterError
        from netbox_nso_plugin.apply_settlement import load_deployment_evidence

        attempt_id = uuid4()
        self._vlan_row(1653, attempt_id)
        self._local_attempt(attempt_id, 87, {"vlan": 427}, answered=False)
        answer = _attempt(attempt_id, self.adapter_device_id, 87, {"vlan": 427}, "pending")
        replayed = False

        class MalformedRefetchSession:
            def request(_self, method, url, **kwargs):
                nonlocal replayed
                if url.endswith("/deployment-evidence"):
                    payload = deployment_wire(_payload(self.adapter_device_id, []))
                    if replayed:
                        payload["attempts"] = {"apply_attempt_id": str(attempt_id)}
                    else:
                        payload["unknown_apply_attempt_ids"] = [str(attempt_id)]
                    return make_response(200, payload)
                self.assertTrue(url.endswith("/actions/apply"))
                replayed = True
                return make_response(202, answer["response"])

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch(
                "netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(MalformedRefetchSession())
            ),
            self.assertRaises(AdapterError) as raised,
        ):
            load_deployment_evidence(self.management)

        self.assertEqual(raised.exception.code, "invalid_response")

    def test_invalid_unknown_attempt_in_replay_refetch_is_rejected(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, load_deployment_evidence

        for unknown_id in ("not-a-uuid", str(uuid4())):
            with self.subTest(unknown_id=unknown_id):
                attempt_id = uuid4()
                local = self._local_attempt(attempt_id, 87, {"vlan": 427}, answered=False)
                answer = _attempt(attempt_id, self.adapter_device_id, 87, {"vlan": 427}, "pending")
                requests = []

                class ReplaySession:
                    def request(_self, method, url, **kwargs):
                        requests.append(url.rsplit("/", 1)[-1])
                        if url.endswith("/deployment-evidence"):
                            payload = _payload(self.adapter_device_id, [])
                            payload["unknown_apply_attempt_ids"] = (
                                [unknown_id] if len(requests) == 3 else [str(attempt_id)]
                            )
                            return make_response(200, deployment_wire(payload))
                        self.assertTrue(url.endswith("/actions/apply"))
                        self.assertEqual(
                            kwargs["json"],
                            {"apply_attempt_id": str(attempt_id), "selected": {"vlan": 427}},
                        )
                        return make_response(202, answer["response"])

                with (
                    patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
                    patch(
                        "netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(ReplaySession())
                    ),
                    self.assertRaisesRegex(
                        EvidenceInvariantError,
                        "invalid_unknown_attempt_uuid"
                        if unknown_id == "not-a-uuid"
                        else "unrequested_unknown_apply_attempt",
                    ),
                ):
                    load_deployment_evidence(self.management, attempt_ids=[attempt_id])

                self.assertEqual(requests, ["deployment-evidence", "apply", "deployment-evidence"])
                local.refresh_from_db()
                self.assertIsNone(local.http_status)
                self.assertIsNone(local.response)

    def test_a_rowless_lost_no_op_response_is_recovered_by_exact_replay(self):
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        attempt_id = uuid4()
        selected = {"ip": 402}
        local = self._local_attempt(attempt_id, 62, selected, answered=False)
        no_op = {
            "device_id": self.adapter_device_id,
            "outcome": "no_op",
            "job_id": None,
            "selected": selected,
            "skipped": {"ip": "already_applied"},
            "skipped_detail": None,
            "generations": [],
        }
        known = {
            "apply_attempt_id": str(attempt_id),
            "admission_state": "admitted",
            "http_status": 200,
            "response": no_op,
            "generations": [],
        }
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
                    return make_response(200, deployment_wire(payload))
                self.assertTrue(url.endswith("/actions/apply"))
                self.assertEqual(
                    body,
                    {"apply_attempt_id": str(attempt_id), "selected": selected},
                )
                admitted = True
                return make_response(200, no_op)

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
            patch("netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(ReplaySession())),
        ):
            evidence = settle_device_apply_attempts(
                self.management,
            )

        self.assertIsNotNone(evidence)
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for _method, url, _body in requests],
            ["deployment-evidence", "apply", "deployment-evidence"],
        )
        local.refresh_from_db()
        self.assertEqual(local.http_status, 200)
        self.assertEqual(local.response, no_op)

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
            )

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_malformed_generation_types_are_non_actionable_contract_errors(self):
        from netbox_nso_plugin.adapter_client import InvalidJobEvidence
        from netbox_nso_plugin.apply_settlement import settle_device_apply_attempts

        attempt_id = uuid4()
        row = self._vlan_row(1636, attempt_id)
        local = self._local_attempt(attempt_id, 52, {"vlan": 302}, answered=False)
        for field, value in (("status", []), ("generations", None)):
            with self.subTest(field=field):
                evidence = _attempt(attempt_id, self.adapter_device_id, 52, {"vlan": 302}, "pending")
                if field == "generations":
                    evidence["generations"] = value
                else:
                    evidence["generations"][0][field] = value

                class EvidenceSession:
                    def request(_self, method, url, **kwargs):
                        return make_response(200, deployment_wire(_payload(self.adapter_device_id, [evidence])))

                with (
                    patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=_CLIENT_CONFIG),
                    patch(
                        "netbox_nso_plugin.adapter_client._get_session", return_value=_epoch_session(EvidenceSession())
                    ),
                    self.assertRaises(InvalidJobEvidence) as raised,
                ):
                    settle_device_apply_attempts(self.management)

                self.assertEqual(raised.exception.reason, "invalid settlement evidence")

                row.refresh_from_db()
                self.assertEqual(row.status, "deploying")
                local.refresh_from_db()
                self.assertIsNone(local.http_status)
                self.assertIsNone(local.response)

    def test_a_rejected_admission_with_null_generations_settles_as_empty(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts
        from netbox_nso_plugin.models import NSOApplyAttempt

        attempt_id = uuid4()
        row = self._vlan_row(1637, attempt_id)
        rejection = {"error": {"code": "apply_unexecutable", "message": "Rejected.", "detail": {}}, "generations": None}
        NSOApplyAttempt.objects.create(
            id=attempt_id,
            adapter_apply_attempt_id=attempt_id,
            management=self.management,
            adapter_device_id=self.adapter_device_id,
            selected={"vlan": 303},
            scope_revisions={"vlan": 1},
            http_status=409,
            response=rejection,
        )
        evidence = {
            "apply_attempt_id": str(attempt_id),
            "admission_state": "rejected",
            "http_status": 409,
            "response": rejection,
            "generations": [],
        }

        settle_apply_attempts(self.management, _payload(self.adapter_device_id, [evidence]))

        row.refresh_from_db()
        self.assertEqual(row.status, "deploying")

    def test_settled_generation_rejects_a_failed_carrier_snapshot(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        attempt_id = uuid4()
        local = self._local_attempt(attempt_id, 84, {"vlan": 424}, answered=False)
        evidence = _attempt(
            attempt_id,
            self.adapter_device_id,
            84,
            {"vlan": 424},
            "settled",
            result={"vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0}},
        )
        evidence["generations"][0]["carrier_job_status"] = "failed"

        with self.assertRaisesRegex(EvidenceInvariantError, "invalid_terminal_batch"):
            settle_apply_attempts(
                self.management,
                _payload(self.adapter_device_id, [evidence]),
            )

        local.refresh_from_db()
        self.assertIsNone(local.http_status)
        self.assertIsNone(local.response)

    def test_settled_generation_rejects_a_missing_carrier_result(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, settle_apply_attempts

        attempt_id = uuid4()
        local = self._local_attempt(attempt_id, 85, {"vlan": 425}, answered=False)
        evidence = _attempt(attempt_id, self.adapter_device_id, 85, {"vlan": 425}, "settled")

        self.assertEqual(evidence["generations"][0]["carrier_job_status"], "succeeded")
        self.assertIsNone(evidence["generations"][0]["carrier_job_result"])

        with self.assertRaisesRegex(EvidenceInvariantError, "invalid_terminal_batch"):
            settle_apply_attempts(
                self.management,
                _payload(self.adapter_device_id, [evidence]),
            )

        local.refresh_from_db()
        self.assertIsNone(local.http_status)
        self.assertIsNone(local.response)

    def test_a_deleted_overlay_is_an_absent_mirror_fragment(self):
        from django.db import transaction

        row = self._vlan_row(1637, uuid4())
        type(row).objects.filter(pk=row.pk).delete()

        from netbox_nso_plugin.intent_state import mirror_refresh
        from netbox_nso_plugin.signals import suppress_intent_push

        with transaction.atomic(), suppress_intent_push(), mirror_refresh(row, {"status"}) as locked:
            self.assertIsNone(locked)
