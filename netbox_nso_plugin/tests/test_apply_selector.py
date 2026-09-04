# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The manual Apply selector contract and its operator-visible outcomes."""

from __future__ import annotations

from uuid import UUID, uuid4

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse
from requests.exceptions import ConnectionError

from ._adapter_http import make_response
from ._outbox_case import (
    ReceiptAdapter,
    content_update,
    make_managed,
    mirror_update,
    own_vlan,
    wait_until_postgres_blocks,
    without_commit_drain,
)
from .mixins import IntentPushResetMixin, _CascadeFlushMixin

_ADAPTER_STREAMS = {
    "bfd",
    "bgp",
    "interface_config",
    "interface_mtu",
    "ip",
    "isis",
    "isis_flex_algo",
    "l2_sap",
    "logging",
    "ospf",
    "route_policy",
    "snmp",
    "static_route",
    "subinterface",
    "svi",
    "vlan",
}


def _promoted(selected):
    # Copied from ../nso-adapter/docs/api-contract.md, actions/apply 202 response,
    # with the fields pinned by ActionApplyGenerationOut in openapi_snapshot.json.
    return {
        "device_id": 1558,
        "outcome": "promoted",
        "job_id": 501,
        "selected": selected,
        "skipped": {},
        "skipped_detail": None,
        "generations": [
            {
                "generation_id": 81,
                "seq": 4,
                "job_id": 501,
                "mode": "networked",
                "source_push_seq": selected,
                "stream_revisions": {stream: 7 for stream in selected},
                "digest": "a" * 64,
            },
            {
                "generation_id": 82,
                "seq": 5,
                "job_id": None,
                "mode": "detach",
                "source_push_seq": selected,
                "stream_revisions": {stream: 7 for stream in selected},
                "digest": "b" * 64,
            },
        ],
    }


def _no_op(selected):
    # Copied from ../nso-adapter/docs/api-contract.md, actions/apply no-op response.
    # The skipped enum is copied from ActionApplyOut in openapi_snapshot.json.
    return {
        "device_id": 1558,
        "outcome": "no_op",
        "selected": selected,
        "skipped": {
            stream: (
                "superseded",
                "already_applied",
                "already_authorized",
                "no_receipt",
                "backfill_only",
                "revision_mismatch",
            )[index % 6]
            for index, stream in enumerate(sorted(selected))
        },
        "skipped_detail": None,
        "generations": [],
    }


class _ApplyContractAdapter(ReceiptAdapter):
    """A strict actions/apply boundary backed by the landed §4.4 receipt shape.

    ReceiptAdapter copies ../nso-adapter/docs/api-contract.md, ``X-Push-Seq`` and
    ``GET /api/v1/intent-receipts``. Each Apply response supplied to this double states
    its contract-source provenance at its definition below.
    """

    def __init__(
        self,
        apply_response,
        *,
        accepted_intent_suffixes=("-intent", "/intent"),
        failed_intent_suffix=None,
        failed_direct_suffix=None,
    ):
        super().__init__()
        self.apply_response = apply_response
        self.accepted_intent_suffixes = accepted_intent_suffixes
        self.failed_intent_suffix = failed_intent_suffix
        self.failed_direct_suffix = failed_direct_suffix
        self.apply_requests: list[dict] = []
        self.direct_requests: list[str] = []

    def _handle(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/actions/apply"):
            body = kwargs.get("json")
            self.apply_requests.append(body)
            if (
                not isinstance(body, dict)
                or set(body) != {"apply_attempt_id", "selected"}
                or not isinstance(body["selected"], dict)
            ):
                # Copied from ../nso-adapter/tests/api/openapi_snapshot.json,
                # actions/apply 422 ErrorEnvelope response.
                return make_response(
                    422,
                    {
                        "error": {
                            "code": "validation_error",
                            "message": "Request validation failed",
                            "detail": {"errors": [{"loc": ["body", "selected"], "type": "missing"}]},
                        }
                    },
                )
            try:
                UUID(body["apply_attempt_id"])
            except (TypeError, ValueError, AttributeError):
                return make_response(
                    422,
                    {
                        "error": {
                            "code": "validation_error",
                            "message": "Request validation failed",
                            "detail": {"errors": [{"loc": ["body", "apply_attempt_id"], "type": "uuid_parsing"}]},
                        }
                    },
                )
            status, payload = self.apply_response(dict(body["selected"]))
            return make_response(status, payload) if payload is not None else make_response(status, content=b"")
        if method == "POST" and url.endswith(("/lag-config/apply", "/switchport/apply")):
            self.direct_requests.append(url)
            if self.failed_direct_suffix and url.endswith(self.failed_direct_suffix):
                # Copied from ../nso-adapter/docs/api-contract.md, direct apply error response.
                return make_response(
                    200,
                    {
                        "status": "error",
                        "message": "Direct configuration failed",
                        "detail": "NSO rejected the snapshot",
                    },
                )
            if url.endswith("/lag-config/apply"):
                # Copied from ../nso-adapter/docs/api-contract.md, lag-config/apply response.
                return make_response(200, {"status": "deployed", "device": "lab-device", "bundle_count": 0})
            # Copied from ../nso-adapter/docs/api-contract.md, switchport/apply response.
            return make_response(200, {"status": "deployed", "device": "lab-device", "interface_count": 0})
        if method == "GET" and url.endswith("/api/v1/jobs/900"):
            # Copied from JobOut in ../nso-adapter/tests/api/openapi_snapshot.json.
            return make_response(
                200,
                {
                    "id": 900,
                    "type": "apply",
                    "device_id": 1558,
                    "status": "running",
                    "result": None,
                    "error": None,
                    "context": None,
                    "created_at": "2026-08-13T10:00:00Z",
                    "updated_at": "2026-08-13T10:00:00Z",
                    "started_at": "2026-08-13T10:00:00Z",
                    "heartbeat_at": "2026-08-13T10:00:00Z",
                    "settle_seq": None,
                },
            )
        if method == "PUT" and self.failed_intent_suffix and url.endswith(self.failed_intent_suffix):
            # Copied from ErrorEnvelope in ../nso-adapter/tests/api/openapi_snapshot.json.
            return make_response(
                500,
                {
                    "error": {
                        "code": "nso_error",
                        "message": "Intent preparation failed",
                        "detail": {},
                    }
                },
            )
        if method == "PUT":
            if not url.endswith(self.accepted_intent_suffixes):
                raise ConnectionError(f"this contract case does not serve {url}")
        return super()._handle(method, url, **kwargs)


class TestApplySelectorFlow(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    """Drive Apply through its URL and the real claim, client, and rollback paths."""

    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser(
            username="apply-selector-admin",
            password="test-password-1558",
            email="apply-selector@test.example",
        )
        self.client.force_login(self.user)
        self.device, self.mgmt = make_managed("apply-selector", 1558)
        self.vlan_state = own_vlan(self.mgmt, 1558, "apply-selector")

    def _create_interface(self, **values):
        with without_commit_drain(), transaction.atomic():
            return Interface.objects.create(**values)

    def _post(self, adapter):
        config, session = adapter.patches()
        url = reverse(
            "plugins:netbox_nso_plugin:nsodevicemanagement_action",
            args=[self.mgmt.pk, "apply"],
        )
        with config, session:
            return self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def _promotion_snapshot(self):
        from types import SimpleNamespace

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOIntentRevision

        registry = delivery.delivery_keys()
        pushed = {}
        for push_seq, entry in enumerate(
            (candidate for candidate in registry.values() if candidate.in_protocol),
            start=1,
        ):
            revision, _created = NSOIntentRevision.objects.get_or_create(
                device=self.device,
                scope=entry.key,
            )
            pushed[entry.key] = SimpleNamespace(revision=revision.revision, push_seq=push_seq)
        return registry, pushed

    def test_promoted_apply_selects_the_store_only_receipt_and_returns_the_whole_chain(self):
        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["job_id"], 501)
        self.assertEqual(result["generations"], _promoted(adapter.apply_requests[0]["selected"])["generations"])
        vlan_url, receipt = next(
            (url, receipt) for url, receipt in adapter.receipts.items() if url.endswith("/vlan-intent")
        )
        self.assertEqual(set(adapter.apply_requests[0]["selected"]), _ADAPTER_STREAMS)
        self.assertEqual(adapter.apply_requests[0]["selected"]["vlan"], receipt["push_seq"])
        self.assertEqual(receipt["params"], {"store_only": "true"})
        pushed = next(request for request in adapter.requests if request["url"] == vlan_url)
        self.assertEqual(pushed["push_seq"], receipt["push_seq"])
        self.assertRegex(receipt["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual([link["digest"] for link in result["generations"]], ["a" * 64, "b" * 64])
        attempt_id = UUID(adapter.apply_requests[0]["apply_attempt_id"])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        self.assertEqual(self.vlan_state.apply_attempt_id, attempt_id)
        from netbox_nso_plugin.models import NSOApplyAttempt

        attempt = NSOApplyAttempt.objects.get(pk=attempt_id)
        self.assertEqual(attempt.selected, adapter.apply_requests[0]["selected"])
        self.assertEqual(attempt.response, _promoted(attempt.selected))

    def test_promoted_retry_moves_apply_failed_intent_back_to_deploying(self):
        mirror_update(
            type(self.vlan_state).objects.get(pk=self.vlan_state.pk),
            status="apply_failed",
            last_apply_at=None,
        )
        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 200)
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        self.assertIsNotNone(self.vlan_state.last_apply_at)

    def test_a_lost_apply_response_keeps_the_exact_attempt_available_for_replay(self):
        def lose_response(_selected):
            raise ConnectionError("response lost after admission")

        adapter = _ApplyContractAdapter(lose_response)

        response = self._post(adapter)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(adapter.apply_requests), 1)
        request = adapter.apply_requests[0]
        from netbox_nso_plugin.models import NSOApplyAttempt

        attempt = NSOApplyAttempt.objects.get(pk=UUID(request["apply_attempt_id"]))
        self.assertEqual(attempt.selected, request["selected"])
        self.assertIsNone(attempt.http_status)
        self.assertIsNone(attempt.response)
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        self.assertEqual(self.vlan_state.apply_attempt_id, attempt.pk)

    def test_no_op_retry_restores_apply_failed_intent(self):
        mirror_update(
            type(self.vlan_state).objects.get(pk=self.vlan_state.pk),
            status="apply_failed",
        )
        adapter = _ApplyContractAdapter(lambda selected: (202, _no_op(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_op")
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "apply_failed")

    def test_promoted_apply_without_a_job_keeps_promoted_rows_deploying(self):
        def promoted_without_job(selected):
            result = _promoted(selected)
            result["job_id"] = None
            for generation in result["generations"]:
                generation["job_id"] = None
            return result

        adapter = _ApplyContractAdapter(lambda selected: (202, promoted_without_job(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["status"], "error")
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        self.assertEqual(
            response.json()["generations"],
            promoted_without_job(adapter.apply_requests[0]["selected"])["generations"],
        )

    def test_malformed_generation_job_chains_keep_prepared_rows_deploying(self):
        def missing_successor_job(selected):
            result = _promoted(selected)
            result["generations"][1].pop("job_id")
            return result

        def successor_claims_the_head_job(selected):
            result = _promoted(selected)
            result["generations"][0]["job_id"] = None
            result["generations"][1]["job_id"] = 501
            return result

        def mismatched_response_job(selected):
            result = _promoted(selected)
            result["job_id"] = 502
            return result

        for malformed in (missing_successor_job, successor_claims_the_head_job, mismatched_response_job):
            with self.subTest(malformed=malformed.__name__):
                mirror_update(
                    type(self.vlan_state).objects.get(pk=self.vlan_state.pk),
                    status="accepted",
                )
                response = self._post(_ApplyContractAdapter(lambda selected: (202, malformed(selected))))

                self.assertEqual(response.status_code, 502)
                self.vlan_state.refresh_from_db()
                self.assertEqual(self.vlan_state.status, "deploying")

    def test_malformed_success_responses_keep_potentially_promoted_rows_deploying(self):
        malformed = (
            {"device_id": 1558, "outcome": "unexpected"},
            {"device_id": 1558, "outcome": "promoted", "generations": []},
            {
                "device_id": 1558,
                "outcome": "promoted",
                "generations": [{"job_id": 501, "stream_revisions": []}],
            },
        )
        for result in malformed:
            with self.subTest(result=result):
                mirror_update(
                    type(self.vlan_state).objects.get(pk=self.vlan_state.pk),
                    status="accepted",
                )
                response = self._post(_ApplyContractAdapter(lambda _selected, result=result: (202, result)))

                self.assertEqual(response.status_code, 502)
                self.vlan_state.refresh_from_db()
                self.assertEqual(self.vlan_state.status, "deploying")

    def test_incomplete_promoted_streams_keep_every_prepared_row_deploying(self):
        def incomplete(selected):
            result = _promoted(selected)
            for generation in result["generations"]:
                generation["stream_revisions"] = {"logging": 7}
            return result

        adapter = _ApplyContractAdapter(lambda selected: (202, incomplete(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 502)
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")

    def test_generation_from_an_unselected_receipt_keeps_prepared_rows_deploying(self):
        def wrong_source(selected):
            result = _promoted(selected)
            for generation in result["generations"]:
                generation["source_push_seq"] = {**selected, "vlan": selected["vlan"] - 1}
            return result

        adapter = _ApplyContractAdapter(lambda selected: (202, wrong_source(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 502)
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")

    def test_a_bad_head_job_releases_the_rows_no_generation_promoted(self):
        """The 502 keeps the promoted rows applying; it must not keep the others too.

        A stream the adapter SKIPPED was prepared locally but has no generation, so nothing
        will ever settle it. Left deploying it reads as applying forever, which is exactly
        what _rollback_prepare_apply exists to prevent; the success path already releases it.
        """
        from netbox_nso_plugin.models import NSOLoggingLevelState

        with without_commit_drain(), transaction.atomic():
            logging_state = NSOLoggingLevelState.objects.create(
                management=self.mgmt,
                console_severity="warning",
                status="accepted",
            )

        def skips_logging_with_a_mismatched_head(selected):
            promoted = {stream: revision for stream, revision in selected.items() if stream != "logging"}
            result = _promoted(promoted)  # generations cover everything but logging
            result["selected"] = dict(selected)  # the echo must repeat the whole selector
            result["skipped"] = {"logging": "no_receipt"}
            result["job_id"] = 502  # disagrees with the head generation's 501
            return result

        response = self._post(
            _ApplyContractAdapter(lambda selected: (202, skips_logging_with_a_mismatched_head(selected)))
        )

        self.assertEqual(response.status_code, 502)
        self.vlan_state.refresh_from_db()
        logging_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying", "a promoted row was released")
        self.assertEqual(logging_state.status, "accepted", "a skipped row was stranded applying")

    def test_deploying_marks_are_atomic_and_apply_is_not_submitted_after_a_database_failure(self):
        from netbox_nso_plugin.models import NSOLoggingLevelState

        with without_commit_drain(), transaction.atomic():
            logging_state = NSOLoggingLevelState.objects.create(
                management=self.mgmt,
                console_severity="warning",
                status="accepted",
            )
        logging_table = NSOLoggingLevelState._meta.db_table

        def reject_logging_update(execute, sql, params, many, context):
            if sql.lstrip().upper().startswith("UPDATE") and f'"{logging_table}"' in sql:
                raise DatabaseError("forced local promotion failure")
            return execute(sql, params, many, context)

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        with connection.execute_wrapper(reject_logging_update):
            response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(adapter.apply_requests, [])
        self.vlan_state.refresh_from_db()
        logging_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")
        self.assertEqual(logging_state.status, "accepted")

    def test_apply_selects_the_stored_isis_receipt_from_the_delivery_registry(self):
        from netbox_nso_plugin.models import NSOISISInterfaceState

        with without_commit_drain(), transaction.atomic():
            interface = self._create_interface(device=self.device, name="Ethernet1", type="1000base-t")
            NSOISISInterfaceState.objects.create(
                management=self.mgmt,
                interface=interface,
                af="ipv4",
                process_tag="CORE",
                status="accepted",
            )
        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 200)
        isis_receipt = next(
            receipt for url, receipt in adapter.receipts.items() if url.endswith("/isis-interface-intent")
        )
        self.assertEqual(adapter.apply_requests[0]["selected"]["isis"], isis_receipt["push_seq"])
        self.assertEqual(isis_receipt["params"], {"store_only": "true"})

    def test_a_raised_preparation_push_aborts_apply_after_a_sibling_succeeds(self):
        from unittest.mock import patch

        from netbox_nso_plugin import drain

        adapter = _ApplyContractAdapter(
            lambda selected: (202, _promoted(selected)),
            accepted_intent_suffixes=("-intent", "/intent"),
        )
        real_push_now = drain.push_now

        def push_now(device_id, scope, **kwargs):
            if scope == "isis":
                raise RuntimeError("IS-IS preparation failed")
            return real_push_now(device_id, scope, **kwargs)

        with patch("netbox_nso_plugin.drain.push_now", side_effect=push_now):
            response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("IS-IS", response.json()["message"])
        self.assertTrue(any(url.endswith("/vlan-intent") for url in adapter.receipts))
        self.assertEqual(adapter.apply_requests, [])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_shared_deadline_expiry_mid_selector_loop_promotes_nothing(self):
        from unittest.mock import patch

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))

        with patch("netbox_nso_plugin.drain._send_clock", lambda: 121 if adapter.receipts else 0):
            response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        self.assertIn("deadline expired", response.json()["message"])
        self.assertEqual(len(adapter.receipts), 1)
        self.assertEqual(adapter.direct_requests, [])
        self.assertEqual(adapter.apply_requests, [])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_a_terminally_failed_preparation_push_aborts_apply_after_a_sibling_succeeds(self):
        adapter = _ApplyContractAdapter(
            lambda selected: (202, _promoted(selected)),
            accepted_intent_suffixes=("-intent", "/intent"),
            failed_intent_suffix="/isis-interface-intent",
        )

        response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("IS-IS", response.json()["message"])
        self.assertTrue(any(url.endswith("/vlan-intent") for url in adapter.receipts))
        self.assertEqual(adapter.apply_requests, [])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_an_intent_change_after_its_receipt_refuses_the_apply(self):
        """A selector and the NetBox rows promoted with it must describe one intent."""
        from unittest.mock import patch

        from netbox_nso_plugin import delivery

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        real_send = delivery.send
        renamed = []

        def send_then_rename(rendered, *args, **kwargs):
            answer = real_send(rendered, *args, **kwargs)
            if rendered.key[1] == "vlan" and not renamed:
                renamed.append(True)
                with without_commit_drain(), transaction.atomic():
                    vlan = self.vlan_state.vlan
                    vlan.name = "intent-changed-after-receipt"
                    vlan.save(update_fields=["name"])
            return answer

        with patch("netbox_nso_plugin.delivery.send", side_effect=send_then_rename):
            response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        self.assertIn("changed during preparation", response.json()["message"])
        self.assertEqual(adapter.apply_requests, [])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_a_rename_after_preparation_repends_the_deploying_vlan(self):
        """A later intent transaction must not inherit an earlier Apply's mark."""
        from netbox_nso_plugin.views import _prepare_apply

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        config, session = adapter.patches()
        with config, session:
            _prepare_apply(self.mgmt)

        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        with without_commit_drain(), transaction.atomic():
            vlan = self.vlan_state.vlan
            vlan.name = "intent-changed-after-preparation"
            vlan.save(update_fields=["name"])

        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_rollback_cannot_release_a_row_repromoted_by_a_later_attempt(self):
        from uuid import uuid4

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.views import _prepare_apply, _rollback_prepare_apply

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        config, session = adapter.patches()
        with config, session:
            prepared, _selected = _prepare_apply(self.mgmt)
        newer_attempt_id = uuid4()
        mirror_update(
            NSOVLANState.objects.get(pk=self.vlan_state.pk),
            apply_attempt_id=newer_attempt_id,
        )

        _rollback_prepare_apply(prepared)

        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        self.assertEqual(self.vlan_state.apply_attempt_id, newer_attempt_id)

    def test_the_inline_vlan_editor_repends_the_deploying_scope(self):
        from netbox_nso_plugin.views import _prepare_apply, _save_vlan_name_edit

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        config, session = adapter.patches()
        with config, session:
            _prepare_apply(self.mgmt)

        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        self.vlan_state.vlan.name = "inline-edit-after-preparation"
        with without_commit_drain():
            _save_vlan_name_edit(self.vlan_state)

        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_an_overlay_edit_repends_its_deploying_scope(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = self._create_interface(device=self.device, name="Ethernet9", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
                apply_attempt_id=uuid4(),
            )

        state.l2_mtu = 1600
        with without_commit_drain():
            _save_owned_overlay_edit(state, "interface_mtu", {"l2_mtu": 1500})

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_a_stale_overlay_instance_cannot_restore_deploying(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = self._create_interface(device=self.device, name="Ethernet9.1", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
                apply_attempt_id=uuid4(),
            )

        stale = NSOInterfaceMtuState.objects.get(pk=state.pk)
        current = NSOInterfaceMtuState.objects.get(pk=state.pk)
        current.l2_mtu = 1600
        with without_commit_drain():
            _save_owned_overlay_edit(current, "interface_mtu", {"l2_mtu": 1500})

        stale.l2_mtu = 1700
        with without_commit_drain():
            _save_owned_overlay_edit(stale, "interface_mtu", {"l2_mtu": 1500})

        stale.refresh_from_db()
        self.assertEqual(stale.status, "accepted")

    def test_a_same_value_stale_overlay_edit_cannot_restore_deploying(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        with transaction.atomic():
            interface = self._create_interface(device=self.device, name="Ethernet9.11", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
                apply_attempt_id=uuid4(),
            )

        first = NSOInterfaceMtuState.objects.get(pk=state.pk)
        stale = NSOInterfaceMtuState.objects.get(pk=state.pk)
        first.l2_mtu = 1600
        with without_commit_drain():
            _save_owned_overlay_edit(first, "interface_mtu", {"l2_mtu": 1500})

        stale.l2_mtu = 1600
        with without_commit_drain():
            _save_owned_overlay_edit(stale, "interface_mtu", {"l2_mtu": 1500})

        stale.refresh_from_db()
        self.assertEqual(stale.status, "accepted")

    def test_a_stale_full_save_cannot_restore_deploying_without_an_intent_change(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = self._create_interface(device=self.device, name="Ethernet9.15", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            stale = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
                apply_attempt_id=uuid4(),
            )
        mirror_update(
            NSOInterfaceMtuState.objects.get(pk=stale.pk),
            status="accepted",
        )

        with without_commit_drain(), transaction.atomic():
            stale.save()

        stale.refresh_from_db()
        self.assertEqual(stale.status, "accepted")

    def test_a_stale_accepted_full_save_cannot_repend_unchanged_deploying_intent(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = self._create_interface(device=self.device, name="Ethernet9.155", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            stale = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="accepted",
            )
        mirror_update(
            NSOInterfaceMtuState.objects.get(pk=stale.pk),
            status="deploying",
            apply_attempt_id=uuid4(),
        )

        with without_commit_drain(), transaction.atomic():
            stale.save()

        stale.refresh_from_db()
        self.assertEqual(stale.status, "deploying")

    def test_a_wire_field_update_repends_an_in_sync_row_when_status_is_not_in_update_fields(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = self._create_interface(device=self.device, name="Ethernet9.16", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="in_sync",
            )

        state.l2_mtu = 1600
        with without_commit_drain(), transaction.atomic():
            state.save(update_fields=["l2_mtu"])

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_a_stale_transient_owned_status_cannot_reclaim_an_unaccepted_row(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        for index, stale_status in enumerate(("deploying", "in_sync", "apply_failed")):
            with self.subTest(status=stale_status):
                interface = self._create_interface(
                    device=self.device,
                    name=f"Ethernet9.2{index}",
                    type="1000base-t",
                )
                with without_commit_drain(), transaction.atomic():
                    stale = NSOInterfaceMtuState.objects.create(
                        management=self.mgmt,
                        interface=interface,
                        l2_mtu=1500,
                        status=stale_status,
                        apply_attempt_id=uuid4() if stale_status == "deploying" else None,
                    )
                content_update(
                    NSOInterfaceMtuState.objects.get(pk=stale.pk),
                    status="imported",
                )

                stale.l2_mtu = 1600
                with without_commit_drain(), transaction.atomic():
                    stale.save()

                stale.refresh_from_db()
                self.assertEqual(stale.status, "changed")

    def test_a_stale_wire_only_update_persists_changed_after_unaccept(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = self._create_interface(device=self.device, name="Ethernet9.29", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            stale = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="in_sync",
            )
        content_update(
            NSOInterfaceMtuState.objects.get(pk=stale.pk),
            status="imported",
        )

        stale.l2_mtu = 1600
        with without_commit_drain(), transaction.atomic():
            stale.save(update_fields=["l2_mtu"])

        stale.refresh_from_db()
        self.assertEqual(stale.status, "changed")

    def test_a_stale_transient_owned_status_cannot_undo_unaccept_when_intent_is_unchanged(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        for index, stale_status in enumerate(("deploying", "in_sync", "apply_failed")):
            with self.subTest(status=stale_status):
                interface = self._create_interface(
                    device=self.device,
                    name=f"Ethernet9.3{index}",
                    type="1000base-t",
                )
                with without_commit_drain(), transaction.atomic():
                    stale = NSOInterfaceMtuState.objects.create(
                        management=self.mgmt,
                        interface=interface,
                        l2_mtu=1500,
                        status=stale_status,
                        apply_attempt_id=uuid4() if stale_status == "deploying" else None,
                    )
                content_update(
                    NSOInterfaceMtuState.objects.get(pk=stale.pk),
                    status="imported",
                )

                with without_commit_drain(), transaction.atomic():
                    stale.save()

                stale.refresh_from_db()
                self.assertEqual(stale.status, "imported")

    def test_suppressed_save_discards_the_previous_intent_change_verdict(self):
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.signals import suppress_intent_push

        interface = self._create_interface(device=self.device, name="Ethernet9.35", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="in_sync",
            )

        state.l2_mtu = 1600
        with without_commit_drain(), transaction.atomic():
            state.save(update_fields=["l2_mtu"])
        self.assertEqual(NSOInterfaceMtuState.objects.get(pk=state.pk).status, "accepted")

        state.status = "in_sync"
        state.last_sync_at = timezone.now()
        with suppress_intent_push(), transaction.atomic():
            state.save(update_fields=["status", "last_sync_at"])

        state.refresh_from_db()
        self.assertEqual(state.status, "in_sync")

    def test_same_row_intent_writers_serialize_before_comparing(self):
        import threading

        from django.db import connections
        from django.db.models.signals import pre_save

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = self._create_interface(device=self.device, name="Ethernet9.2", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
                apply_attempt_id=uuid4(),
            )
        stale = NSOInterfaceMtuState.objects.get(pk=state.pk)
        first_locked = threading.Event()
        release_first = threading.Event()
        comparison_finished = threading.Event()
        errors = []

        def after_intent_comparison(sender, instance, **kwargs):
            if instance.pk == state.pk:
                comparison_finished.set()

        def hold_first():
            try:
                current = NSOInterfaceMtuState.objects.get(pk=state.pk)
                with intent_transaction(footprint_for_instance(current)):
                    NSOInterfaceMtuState.objects.select_for_update().filter(pk=state.pk).update(
                        l2_mtu=1600,
                        status="accepted",
                    )
                    comparison_finished.clear()
                    first_locked.set()
                    if not release_first.wait(10):
                        raise AssertionError("the competing save did not inspect the locked row")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def save_stale():
            try:
                if not first_locked.wait(10):
                    raise AssertionError("the first writer did not lock the row")
                with without_commit_drain():
                    _save_owned_overlay_edit(stale, "interface_mtu", {"l2_mtu": 1500})
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        pre_save.connect(after_intent_comparison, sender=NSOInterfaceMtuState, weak=False)
        holder = threading.Thread(target=hold_first)
        writer = threading.Thread(target=save_stale)
        holder.start()
        writer.start()
        try:
            self.assertTrue(first_locked.wait(10), "the first writer did not lock the row")
            self.assertFalse(comparison_finished.wait(1), "the stale writer compared before it acquired the row lock")
        finally:
            release_first.set()
            holder.join(10)
            writer.join(10)
            pre_save.disconnect(after_intent_comparison, sender=NSOInterfaceMtuState)

        self.assertFalse(holder.is_alive())
        self.assertFalse(writer.is_alive())
        if errors:
            raise errors[0]
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_accept_derives_status_from_the_locked_current_row(self):
        import threading
        import time

        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.db import connections

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction, mirror_transaction
        from netbox_nso_plugin.models import NSOBFDInterfaceState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.views import NSOBFDInterfaceStateAcceptView

        interface = self._create_interface(device=self.device, name="Ethernet9.4", type="1000base-t")
        state = NSOBFDInterfaceState(
            management=self.mgmt,
            interface=interface,
            min_tx=300,
            min_rx=300,
            multiplier=3,
            status="imported",
        )
        with suppress_intent_push(), mirror_transaction(footprint_for_instance(state)):
            state.save(force_insert=True)

        row_updated = threading.Event()
        accept_waited = threading.Event()
        errors = []
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            accept_pid = cursor.fetchone()[0]

        def promote_row():
            try:
                with transaction.atomic():
                    current = NSOBFDInterfaceState.objects.get(pk=state.pk)
                    with intent_transaction(footprint_for_instance(current)):
                        current.status = "deploying"
                        current.apply_attempt_id = uuid4()
                        current.save(update_fields=["status", "apply_attempt_id"])
                    row_updated.set()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)",
                                [accept_pid],
                            )
                            if cursor.fetchone()[0]:
                                accept_waited.set()
                                break
                        time.sleep(0.01)
                    if not accept_waited.is_set():
                        raise AssertionError("the accept view did not wait for the concurrent row update")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises the worker failure)
                errors.append(exc)
            finally:
                connections.close_all()

        updater = threading.Thread(target=promote_row)
        updater.start()
        self.assertTrue(row_updated.wait(10), "the concurrent promotion did not update the accepted row")
        request = RequestFactory().post("/")
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        with suppress_intent_push():
            response = NSOBFDInterfaceStateAcceptView().post(request, state.pk)
        updater.join(10)

        self.assertTrue(accept_waited.is_set(), "the accept view did not wait for the concurrent row update")
        self.assertFalse(updater.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_interface_rename_locks_the_native_row_before_related_intent_rows(self):
        import threading

        from dcim.models import Interface
        from django.db import connections
        from django.db.models.signals import pre_save

        from netbox_nso_plugin.intent_state import footprint_for_instance, intent_transaction
        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = self._create_interface(device=self.device, name="Ethernet9.39", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="accepted",
            )
        native_locked = threading.Event()
        allow_writer = threading.Event()
        rename_passed_pre_save = threading.Event()
        errors = []

        def after_rename_lock(sender, instance, update_fields=None, **kwargs):
            if instance.pk == interface.pk and update_fields is not None and "name" in update_fields:
                rename_passed_pre_save.set()

        def edit_overlay():
            try:
                current_interface = Interface.objects.get(pk=interface.pk)
                with intent_transaction(footprint_for_instance(current_interface)):
                    native_locked.set()
                    if not allow_writer.wait(10):
                        raise AssertionError("the rename did not inspect the native lock")
                    current = NSOInterfaceMtuState.objects.get(pk=state.pk)
                    current.l2_mtu = 1600
                    with without_commit_drain():
                        _save_owned_overlay_edit(current, "interface_mtu", {"l2_mtu": 1500})
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def rename_interface():
            try:
                if not native_locked.wait(10):
                    raise AssertionError("the native writer did not lock the interface")
                with without_commit_drain(), transaction.atomic():
                    current = Interface.objects.get(pk=interface.pk)
                    current.name = "Ethernet9.390"
                    current.save(update_fields=["name"])
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        pre_save.connect(after_rename_lock, sender=Interface, weak=False)
        writer = threading.Thread(target=edit_overlay)
        renamer = threading.Thread(target=rename_interface)
        writer.start()
        renamer.start()
        try:
            self.assertTrue(native_locked.wait(10), "the native writer did not lock the interface")
            locked_intent_first = rename_passed_pre_save.wait(1)
        finally:
            allow_writer.set()
            writer.join(10)
            renamer.join(10)
            pre_save.disconnect(after_rename_lock, sender=Interface)

        self.assertFalse(locked_intent_first, "the rename locked intent before the native interface")
        self.assertFalse(writer.is_alive())
        self.assertFalse(renamer.is_alive())
        if errors:
            raise errors[0]

    def test_interface_rename_locks_rows_referenced_by_indirect_dependencies_before_mutation(self):
        import threading

        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType
        from django.db import connections
        from django.db.models.signals import pre_save
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        from netbox_nso_plugin.models import NSOBGPPeerState, NSOInterfaceIPState

        with without_commit_drain(), transaction.atomic():
            interface = self._create_interface(device=self.device, name="Ethernet9.395", type="lag")
            child = self._create_interface(
                device=self.device,
                name="LAG395:395",
                type="virtual",
                parent=interface,
            )
            rir = RIR.objects.create(name="Rename fence private ASNs", slug="rename-fence-private-asns")
            local_as = ASN.objects.create(asn=65042, rir=rir)
            remote_as = ASN.objects.create(asn=65043, rir=rir)
            router = BGPRouter.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(type(self.device)),
                assigned_object_id=self.device.pk,
                asn=local_as,
                name="65042",
            )
            scope = BGPScope.objects.create(router=router)
            peer_address = IPAddress.objects.create(address="198.18.95.2/32")
            peer = BGPPeer.objects.create(
                scope=scope,
                peer=peer_address,
                remote_as=remote_as,
                update_source=interface,
                enabled=True,
            )
        unlinked_bgp_state = NSOBGPPeerState.objects.get(bgp_peer=peer)
        content_update(unlinked_bgp_state, bgp_peer=None)
        rename_prepared = threading.Event()
        release_rename = threading.Event()
        ip_dependency_created = threading.Event()
        bgp_dependency_created = threading.Event()
        errors = []

        def hold_rename_after_dependency_capture(sender, instance, update_fields=None, **kwargs):
            if instance.pk == interface.pk and update_fields is not None and "name" in update_fields:
                rename_prepared.set()
                if not release_rename.wait(10):
                    raise AssertionError("the dependency writer did not inspect the native lock")

        def rename_interface():
            try:
                with without_commit_drain(), transaction.atomic():
                    current = Interface.objects.get(pk=interface.pk)
                    current.name = "Ethernet9.396"
                    current.save(update_fields=["name"])
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def create_ip_dependency():
            try:
                if not rename_prepared.wait(10):
                    raise AssertionError("the rename did not reach its dependency fence")
                with without_commit_drain(), transaction.atomic():
                    NSOInterfaceIPState.objects.create(
                        interface_id=child.pk,
                        address="198.18.95.1/31",
                        status="accepted",
                    )
                ip_dependency_created.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def relink_bgp_dependency():
            try:
                if not rename_prepared.wait(10):
                    raise AssertionError("the rename did not reach its dependency fence")
                with without_commit_drain(), transaction.atomic():
                    current = NSOBGPPeerState.objects.get(pk=unlinked_bgp_state.pk)
                    current.bgp_peer_id = peer.pk
                    current.save(update_fields=["bgp_peer"])
                bgp_dependency_created.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        pre_save.connect(hold_rename_after_dependency_capture, sender=Interface, weak=False)
        renamer = threading.Thread(target=rename_interface)
        ip_creator = threading.Thread(target=create_ip_dependency)
        bgp_creator = threading.Thread(target=relink_bgp_dependency)
        renamer.start()
        ip_creator.start()
        bgp_creator.start()
        try:
            self.assertTrue(rename_prepared.wait(10), "the rename did not reach its dependency fence")
            ip_created_before_rename = ip_dependency_created.wait(1)
            bgp_created_before_rename = bgp_dependency_created.wait(1)
        finally:
            release_rename.set()
            renamer.join(10)
            ip_creator.join(10)
            bgp_creator.join(10)
            pre_save.disconnect(hold_rename_after_dependency_capture, sender=Interface)

        self.assertFalse(ip_created_before_rename, "an IP dependency was inserted before the rename committed")
        self.assertFalse(bgp_created_before_rename, "a BGP dependency was linked before the rename committed")
        self.assertFalse(renamer.is_alive())
        self.assertFalse(ip_creator.is_alive())
        self.assertFalse(bgp_creator.is_alive())
        if errors:
            raise errors[0]

    def test_vlan_rename_fences_a_new_device_attachment_before_dependency_capture(self):
        import threading

        from django.db import connections
        from django.db.models.signals import pre_save
        from django.test import Client
        from ipam.models import VLAN

        from netbox_nso_plugin.intent_state import footprint_for_instance, mirror_transaction
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push

        with transaction.atomic():
            vlan = VLAN.objects.create(vid=3558, name="before-rename")
        state = NSOVLANState(management=self.mgmt, vlan=vlan, device_name=vlan.name, status="imported")
        with suppress_intent_push(), mirror_transaction(footprint_for_instance(state)):
            state.save(force_insert=True)
        other_device, other_mgmt = make_managed("apply-selector-vlan-attach", 2558)
        attach_client = Client()
        attach_client.force_login(self.user)
        rename_prepared = threading.Event()
        release_rename = threading.Event()
        attachment_reached_save = threading.Event()
        attachment_done = threading.Event()
        errors = []

        def hold_rename_after_dependency_capture(sender, instance, update_fields=None, **kwargs):
            if instance.pk == vlan.pk and update_fields is not None and "name" in update_fields:
                rename_prepared.set()
                if not release_rename.wait(10):
                    raise AssertionError("the attachment did not inspect the VLAN fence")

        def note_attachment_save(sender, instance, **kwargs):
            if instance.management_id == other_mgmt.pk and instance.vlan_id == vlan.pk:
                attachment_reached_save.set()

        def rename_vlan():
            try:
                with without_commit_drain(), transaction.atomic():
                    current = VLAN.objects.get(pk=vlan.pk)
                    current.name = "after-rename"
                    current.save(update_fields=["name"])
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def attach_vlan():
            try:
                if not rename_prepared.wait(10):
                    raise AssertionError("the rename did not reach its dependency fence")
                with without_commit_drain():
                    response = attach_client.post(
                        reverse("plugins:netbox_nso_plugin:vlan_attach", args=[other_device.pk]),
                        {"vlan": vlan.pk},
                    )
                if response.status_code != 302:
                    raise AssertionError(f"VLAN attachment returned HTTP {response.status_code}")
                attachment_done.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        pre_save.connect(hold_rename_after_dependency_capture, sender=VLAN, weak=False)
        pre_save.connect(note_attachment_save, sender=NSOVLANState, weak=False)
        renamer = threading.Thread(target=rename_vlan)
        attacher = threading.Thread(target=attach_vlan)
        renamer.start()
        attacher.start()
        try:
            self.assertTrue(rename_prepared.wait(10), "the rename did not reach its dependency fence")
            attachment_saved_before_rename = attachment_reached_save.wait(5)
            attached_before_rename = attachment_done.wait(1)
        finally:
            release_rename.set()
            renamer.join(10)
            attacher.join(10)
            pre_save.disconnect(hold_rename_after_dependency_capture, sender=VLAN)
            pre_save.disconnect(note_attachment_save, sender=NSOVLANState)

        self.assertFalse(attachment_saved_before_rename, "a new VLAN attachment reached its save before the rename")
        self.assertFalse(attached_before_rename, "a new VLAN attachment committed before the rename")
        self.assertFalse(renamer.is_alive())
        self.assertFalse(attacher.is_alive())
        if errors:
            raise errors[0]
        state = NSOVLANState.objects.get(management=other_mgmt, vlan=vlan)
        self.assertEqual(state.status, "accepted")
        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "after-rename")

    def test_vlan_attachment_and_rename_use_the_same_lock_order(self):
        import threading
        from unittest.mock import patch

        from django.db import connections
        from django.test import Client
        from ipam.models import VLAN

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.intent_state import (
            IntentMutationProtocolError,
            footprint_for_instance,
            mirror_transaction,
        )
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push

        with transaction.atomic():
            vlan = VLAN.objects.create(vid=3557, name="attach-before-rename")
        state = NSOVLANState(management=self.mgmt, vlan=vlan, device_name=vlan.name, status="imported")
        with suppress_intent_push(), mirror_transaction(footprint_for_instance(state)):
            state.save(force_insert=True)
        other_device, other_mgmt = make_managed("apply-selector-attach-order", 2557)
        attach_client = Client()
        attach_client.force_login(self.user)
        attach_holds_vlan = threading.Event()
        release_attach = threading.Event()
        rename_acquired_vlan = threading.Event()
        renamer_ready = threading.Event()
        renamer_pid: list[int] = []
        errors = []
        original_shared_lock = apply_state.lock_shared_dependencies
        attacher = None
        renamer = None

        def observe_shared_lock(keys):
            original_shared_lock(keys)
            if ("vlan", str(vlan.pk)) not in keys:
                return
            if threading.current_thread() is attacher:
                attach_holds_vlan.set()
                if not release_attach.wait(10):
                    raise AssertionError("the VLAN attachment was not released")
            elif threading.current_thread() is renamer:
                rename_acquired_vlan.set()

        def attach_vlan():
            try:
                with without_commit_drain():
                    response = attach_client.post(
                        reverse("plugins:netbox_nso_plugin:vlan_attach", args=[other_device.pk]),
                        {"vlan": vlan.pk},
                    )
                if response.status_code != 302:
                    raise AssertionError(f"VLAN attachment returned HTTP {response.status_code}")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def rename_vlan():
            try:
                if not attach_holds_vlan.wait(10):
                    raise AssertionError("the attachment did not acquire the shared VLAN lock")
                with without_commit_drain(), transaction.atomic():
                    current = VLAN.objects.get(pk=vlan.pk)
                    current.name = "rename-after-attach-started"
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        renamer_pid.append(cursor.fetchone()[0])
                    renamer_ready.set()
                    current.save(update_fields=["name"])
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        with (
            patch("netbox_nso_plugin.apply_state.lock_shared_dependencies", side_effect=observe_shared_lock),
        ):
            attacher = threading.Thread(target=attach_vlan)
            renamer = threading.Thread(target=rename_vlan)
            attacher.start()
            self.assertTrue(attach_holds_vlan.wait(10), "the attachment did not acquire the shared VLAN lock")
            renamer.start()
            try:
                self.assertTrue(renamer_ready.wait(10), "the rename did not reach its database write")
                wait_until_postgres_blocks(renamer_pid[0], "the rename")
                self.assertFalse(rename_acquired_vlan.is_set(), "the rename bypassed the attachment's VLAN lock")
            finally:
                release_attach.set()
                attacher.join(10)
                renamer.join(10)
            self.assertTrue(rename_acquired_vlan.is_set(), "the rename never acquired the released VLAN lock")

        self.assertFalse(attacher.is_alive())
        self.assertFalse(renamer.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], IntentMutationProtocolError)
        self.assertIn("changed its renderer targets", str(errors[0]))
        self.assertTrue(NSOVLANState.objects.filter(management=other_mgmt, vlan=vlan).exists())
        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "attach-before-rename")

    def test_native_vlan_prelocks_leave_malformed_payloads_to_scope_isolation(self):
        from netbox_nso_plugin.intent_state import MutationFootprint
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.svi_reconciler import svi_reconcile_footprint
        from netbox_nso_plugin.vlan_reconciler import (
            switchport_reconcile_footprint,
            vlan_reconcile_footprint,
        )

        state_before = list(NSOVLANState.objects.filter(management=self.mgmt).values_list("pk", "vlan_id", "status"))
        with transaction.atomic():
            results = [
                vlan_reconcile_footprint(
                    self.device,
                    {"vlans": [{"vlan_id": "not-an-integer"}, None]},
                ),
                svi_reconcile_footprint(
                    self.device,
                    {"interfaces": [{"vlan_id": "not-an-integer"}, None]},
                ),
                switchport_reconcile_footprint(
                    self.device,
                    {"interfaces": [{"untagged_vlan": "bad", "tagged_vlans": 5}, None]},
                ),
                vlan_reconcile_footprint(self.device, {"vlans": 5}),
                svi_reconcile_footprint(self.device, {"interfaces": 5}),
                switchport_reconcile_footprint(self.device, {"interfaces": 5}),
            ]

        self.assertTrue(all(isinstance(result, MutationFootprint) for result in results))
        self.assertEqual(
            list(NSOVLANState.objects.filter(management=self.mgmt).values_list("pk", "vlan_id", "status")),
            state_before,
        )

    def test_unknown_native_vlan_dependency_family_is_rejected(self):
        from netbox_nso_plugin.reconcile import _native_vlan_footprint

        with self.assertRaisesRegex(ValueError, "unknown native VLAN dependency family"):
            _native_vlan_footprint(self.device, {}, "unknown")

    def test_all_native_vlan_dependency_prelocks_defer_discovery_to_one_lock_sequence(self):

        from netbox_nso_plugin.intent_state import MutationFootprint, footprint_for_instance, mirror_transaction
        from netbox_nso_plugin.models import NSOSVIState, NSOSwitchportState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.svi_reconciler import svi_reconcile_footprint
        from netbox_nso_plugin.vlan_reconciler import (
            switchport_reconcile_footprint,
            vlan_reconcile_footprint,
        )

        interface = self._create_interface(device=self.device, name="Vlan3558", type="virtual")
        svi_state = NSOSVIState(
            management=self.mgmt,
            interface=interface,
            vlan=self.vlan_state.vlan,
            status="imported",
        )
        switchport_state = NSOSwitchportState(
            management=self.mgmt,
            interface=interface,
            mode="access",
            untagged_vlan=self.vlan_state.vlan,
            status="imported",
        )
        footprint = MutationFootprint.merge(
            footprint_for_instance(svi_state),
            footprint_for_instance(switchport_state),
        )
        with suppress_intent_push(), mirror_transaction(footprint):
            svi_state.save(force_insert=True)
            switchport_state.save(force_insert=True)

        footprints = [
            vlan_reconcile_footprint(self.device, {"vlans": [{"vlan_id": self.vlan_state.vlan.vid}]}),
            svi_reconcile_footprint(self.device, {"interfaces": [{"vlan_id": self.vlan_state.vlan.vid}]}),
            switchport_reconcile_footprint(
                self.device,
                {"interfaces": [{"untagged_vlan": self.vlan_state.vlan.vid, "tagged_vlans": []}]},
            ),
        ]

        for footprint in footprints:
            self.assertIn(("vlan", str(self.vlan_state.vlan_id)), footprint.shared_keys)
            self.assertIn(
                ("ipam.vlan", self.vlan_state.vlan_id), {(row.model_label, row.pk) for row in footprint.source_rows}
            )

    def test_advisory_lock_helpers_use_the_two_declared_transaction_namespaces(self):
        from netbox_nso_plugin.apply_state import lock_device_intent_transaction, lock_shared_dependencies

        lock_id = 1611
        namespaces = [1_503_003_007, 1_503_003_008]

        with transaction.atomic():
            lock_device_intent_transaction(lock_id)
            lock_shared_dependencies({("vlan", str(lock_id))})
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT classid::bigint "
                    "FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory' "
                    "AND classid = ANY(%s) ORDER BY classid",
                    [namespaces],
                )
                held_namespaces = {row[0] for row in cursor.fetchall()}

        self.assertEqual(held_namespaces, set(namespaces))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory' "
                "AND classid = ANY(%s) AND objid = %s",
                [namespaces, lock_id],
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_quiescence_allows_an_unmanaged_vlan_edit(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.deployment import quiesce, resume

        with transaction.atomic():
            vlan = VLAN.objects.create(vid=3556, name="outside-nso-intent")

        quiesce()
        try:
            with transaction.atomic():
                vlan.name = "still-outside-nso-intent"
                vlan.save(update_fields=["name"])
        finally:
            resume()

        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "still-outside-nso-intent")

    def test_switchport_dependency_discovery_fences_a_new_vlan_attachment(self):
        import threading

        from django.db import connections
        from django.db.models.signals import pre_save
        from django.test import Client
        from ipam.models import VLAN

        from netbox_nso_plugin.intent_state import intent_transaction
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import switchport_reconcile_footprint

        with transaction.atomic():
            vlan = VLAN.objects.create(vid=3559, name="membership-fence")
        attach_client = Client()
        attach_client.force_login(self.user)
        discovery_complete = threading.Event()
        release_discovery = threading.Event()
        attachment_reached_save = threading.Event()
        errors = []

        def note_attachment_save(sender, instance, **kwargs):
            if instance.management_id == self.mgmt.pk and instance.vlan_id == vlan.pk:
                attachment_reached_save.set()

        def hold_dependency_snapshot():
            try:
                footprint = switchport_reconcile_footprint(
                    self.device,
                    {"interfaces": [{"untagged_vlan": vlan.vid, "tagged_vlans": []}]},
                )
                with intent_transaction(footprint):
                    discovery_complete.set()
                    if not release_discovery.wait(10):
                        raise AssertionError("the attachment did not inspect the membership fence")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def attach_vlan():
            try:
                if not discovery_complete.wait(10):
                    raise AssertionError("switchport dependency discovery did not complete")
                with without_commit_drain():
                    response = attach_client.post(
                        reverse("plugins:netbox_nso_plugin:vlan_attach", args=[self.device.pk]),
                        {"vlan": vlan.pk},
                    )
                if response.status_code != 302:
                    raise AssertionError(f"VLAN attachment returned HTTP {response.status_code}")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        pre_save.connect(note_attachment_save, sender=NSOVLANState, weak=False)
        holder = threading.Thread(target=hold_dependency_snapshot)
        attacher = threading.Thread(target=attach_vlan)
        holder.start()
        attacher.start()
        try:
            self.assertTrue(discovery_complete.wait(10), "switchport dependency discovery did not complete")
            attachment_saved_during_snapshot = attachment_reached_save.wait(5)
        finally:
            release_discovery.set()
            holder.join(10)
            attacher.join(10)
            pre_save.disconnect(note_attachment_save, sender=NSOVLANState)

        self.assertFalse(
            attachment_saved_during_snapshot,
            "a VLAN attachment changed membership after switchport dependency discovery",
        )
        self.assertFalse(holder.is_alive())
        self.assertFalse(attacher.is_alive())
        if errors:
            raise errors[0]

    def test_switchport_dependency_discovery_fences_a_vlan_rescope_merge(self):
        import threading

        from django.db import connections
        from django.db.models.signals import pre_save
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.intent_state import intent_transaction
        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.vlan_reconciler import (
            rescope_vlan,
            switchport_reconcile_footprint,
        )

        target_group = VLANGroup.objects.create(name="Membership fence target", slug="membership-fence-target")
        with transaction.atomic():
            target_vlan = VLAN.objects.create(
                group=target_group,
                vid=self.vlan_state.vlan.vid,
                name="shared-target",
            )
        svi_interface = self._create_interface(device=self.device, name="Vlan3559", type="virtual")
        with without_commit_drain(), transaction.atomic():
            svi_state = NSOSVIState.objects.create(
                management=self.mgmt,
                interface=svi_interface,
                vlan=self.vlan_state.vlan,
                status="accepted",
            )
        discovery_complete = threading.Event()
        release_discovery = threading.Event()
        rescope_reached_state_save = threading.Event()
        rescope_done = threading.Event()
        errors = []

        def note_rescope_save(sender, instance, **kwargs):
            if instance.pk == self.vlan_state.pk and instance.vlan_id == target_vlan.pk:
                rescope_reached_state_save.set()

        def hold_dependency_snapshot():
            try:
                footprint = switchport_reconcile_footprint(
                    self.device,
                    {"interfaces": [{"untagged_vlan": self.vlan_state.vlan.vid, "tagged_vlans": []}]},
                )
                with intent_transaction(footprint):
                    discovery_complete.set()
                    if not release_discovery.wait(30):
                        raise AssertionError("rescope did not inspect the membership fence")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def merge_vlan():
            try:
                if not discovery_complete.wait(10):
                    raise AssertionError("switchport dependency discovery did not complete")
                rescope_vlan(type(self.vlan_state).objects.get(pk=self.vlan_state.pk), target_group)
                rescope_done.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        holder = threading.Thread(target=hold_dependency_snapshot)
        rescoping = threading.Thread(target=merge_vlan)
        pre_save.connect(note_rescope_save, sender=type(self.vlan_state), weak=False)
        holder.start()
        rescoping.start()
        try:
            self.assertTrue(discovery_complete.wait(10), "switchport dependency discovery did not complete")
            rescope_saved_during_snapshot = rescope_reached_state_save.wait(5)
            rescope_finished_during_snapshot = rescope_done.wait(5)
        finally:
            release_discovery.set()
            holder.join(10)
            rescoping.join(10)
            pre_save.disconnect(note_rescope_save, sender=type(self.vlan_state))

        self.assertFalse(
            rescope_saved_during_snapshot,
            "VLAN rescope published new membership after switchport dependency discovery",
        )
        self.assertFalse(
            rescope_finished_during_snapshot,
            "VLAN rescope changed membership after switchport dependency discovery",
        )
        self.assertFalse(holder.is_alive())
        self.assertFalse(rescoping.is_alive())
        if errors:
            raise errors[0]
        self.vlan_state.refresh_from_db()
        svi_state.refresh_from_db()
        self.assertEqual(self.vlan_state.vlan_id, target_vlan.pk)
        self.assertEqual(svi_state.vlan_id, target_vlan.pk)

    def test_vlan_rescope_target_slot_serializes_a_concurrent_vlan_creation(self):
        import threading
        from unittest.mock import patch

        from django.db import IntegrityError, connections
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.vlan_reconciler import rescope_vlan

        target_group = VLANGroup.objects.create(name="Concurrent target", slug="concurrent-target")
        source_vid = self.vlan_state.vlan.vid
        rescope_admitted = threading.Event()
        release_rescope = threading.Event()
        target_created = threading.Event()
        rescope_moved = threading.Event()
        rescope_errors = []
        creator_errors = []
        original_lock = apply_state.lock_shared_dependencies

        def hold_target_slot(keys):
            original_lock(keys)
            if threading.current_thread() is rescoping and ("vlan-slot", f"{target_group.pk}:{source_vid}") in keys:
                rescope_admitted.set()
                if not release_rescope.wait(10):
                    raise AssertionError("the concurrent VLAN creator did not inspect the target-group fence")

        def move_vlan():
            try:
                rescope_vlan(type(self.vlan_state).objects.get(pk=self.vlan_state.pk), target_group)
                rescope_moved.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                rescope_errors.append(exc)
            finally:
                connections.close_all()

        def create_target_vlan():
            try:
                if not rescope_admitted.wait(10):
                    raise AssertionError("the rescope did not reach its target lookup fence")
                with transaction.atomic():
                    VLAN.objects.create(group=target_group, vid=source_vid, name="concurrent")
                target_created.set()
            except IntegrityError as exc:
                creator_errors.append(exc)
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                creator_errors.append(exc)
            finally:
                connections.close_all()

        with patch(
            "netbox_nso_plugin.apply_state.lock_shared_dependencies",
            side_effect=hold_target_slot,
        ):
            rescoping = threading.Thread(target=move_vlan)
            creator = threading.Thread(target=create_target_vlan)
            rescoping.start()
            creator.start()
            try:
                self.assertTrue(rescope_admitted.wait(10), "the rescope did not reach its target lookup fence")
                created_before_rescope = target_created.wait(1)
            finally:
                release_rescope.set()
                rescoping.join(10)
                creator.join(10)

        self.assertFalse(created_before_rescope, "the concurrent target VLAN bypassed the target-slot lock")
        if rescope_errors:
            raise rescope_errors[0]
        self.assertEqual(len(creator_errors), 1)
        self.assertIsInstance(creator_errors[0], IntegrityError)
        self.assertTrue(rescope_moved.is_set(), "the rescope did not complete before the competing insert")
        self.assertFalse(rescoping.is_alive())
        self.assertFalse(creator.is_alive())
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.vlan.group_id, target_group.pk)
        self.assertEqual(VLAN.objects.filter(group=target_group, vid=source_vid).count(), 1)

    def test_vlan_rescope_merge_preserves_a_concurrent_accept(self):
        import threading
        from unittest.mock import patch

        from django.db import connections, transaction
        from django.test import Client
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import views
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.status_machine import is_owned
        from netbox_nso_plugin.vlan_reconciler import rescope_vlan

        source_vlan = self.vlan_state.vlan
        target_group = VLANGroup.objects.create(name="Accept target", slug="accept-target")
        with transaction.atomic():
            target_vlan = VLAN.objects.create(group=target_group, vid=source_vlan.vid, name=source_vlan.name)
        content_update(
            NSOVLANState.objects.get(pk=self.vlan_state.pk),
            status="imported",
        )
        with without_commit_drain(), transaction.atomic():
            target_state = NSOVLANState.objects.create(
                management=self.mgmt,
                vlan=target_vlan,
                device_name=source_vlan.name,
                status="imported",
            )
        accept_prepared = threading.Event()
        release_accept = threading.Event()
        rescope_done = threading.Event()
        errors = []
        original_status_after_accept = views._status_after_accept

        def hold_accept_before_save(status):
            accepted = original_status_after_accept(status)
            accept_prepared.set()
            if not release_accept.wait(10):
                raise AssertionError("the rescope did not inspect the accepted VLAN row")
            return accepted

        def accept_source():
            client = Client()
            client.force_login(self.user)
            try:
                with suppress_intent_push():
                    response = client.post(
                        reverse("plugins:netbox_nso_plugin:vlan_accept", args=[self.vlan_state.pk]),
                    )
                if response.status_code != 302:
                    raise AssertionError(f"VLAN accept returned HTTP {response.status_code}")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def merge_vlan():
            try:
                if not accept_prepared.wait(10):
                    raise AssertionError("the accept did not reach its save fence")
                with suppress_intent_push(), transaction.atomic():
                    rescope_vlan(NSOVLANState.objects.get(pk=self.vlan_state.pk), target_group)
                rescope_done.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        with patch("netbox_nso_plugin.views._status_after_accept", side_effect=hold_accept_before_save):
            accepting = threading.Thread(target=accept_source)
            rescoping = threading.Thread(target=merge_vlan)
            accepting.start()
            self.assertTrue(accept_prepared.wait(10), "the accept did not reach its save fence")
            rescoping.start()
            try:
                rescope_finished_during_accept = rescope_done.wait(1)
            finally:
                release_accept.set()
                accepting.join(10)
                rescoping.join(10)

        self.assertFalse(rescope_finished_during_accept, "rescope deleted a VLAN row while Accept was saving it")
        self.assertFalse(accepting.is_alive())
        self.assertFalse(rescoping.is_alive())
        if errors:
            raise errors[0]
        target_state.refresh_from_db()
        self.assertTrue(is_owned(target_state.status))
        self.assertFalse(NSOVLANState.objects.filter(pk=self.vlan_state.pk).exists())

    def test_switchport_accept_rejects_an_overlay_owned_by_the_interfaces_old_device(self):
        from netbox_nso_plugin.intent_state import offline_mutation
        from netbox_nso_plugin.models import NSOSwitchportState
        from netbox_nso_plugin.views import _reload_switchport_accept_state, _SwitchportAcceptRetry

        other_device, _other_management = make_managed("switchport-accept-rescoped", 9379)
        interface = self._create_interface(device=self.device, name="Ethernet9.381", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=interface,
                mode="access",
                status="imported",
            )
        with transaction.atomic(), offline_mutation():
            Interface.objects.filter(pk=interface.pk).update(device=other_device)

        with self.assertRaises(_SwitchportAcceptRetry):
            _reload_switchport_accept_state(state, set())

    def test_switchport_accept_retries_a_device_move_during_footprint_acquisition(self):
        import threading
        from unittest.mock import patch

        from django.db import connections

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.intent_state import offline_mutation
        from netbox_nso_plugin.models import NSOSwitchportState

        other_device, _other_management = make_managed("switchport-accept-moved", 9380)
        interface = self._create_interface(device=self.device, name="Ethernet9.382", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=interface,
                mode="access",
                status="imported",
            )
        original_lock = apply_state.lock_device_intent_transaction
        moved = []

        def lock_then_move(device_id):
            original_lock(device_id)
            if moved or device_id != self.device.pk:
                return

            def move_interface():
                try:
                    with transaction.atomic(), offline_mutation():
                        Interface.objects.filter(pk=interface.pk).update(device=other_device)
                finally:
                    connections.close_all()

            mover = threading.Thread(target=move_interface)
            mover.start()
            mover.join(10)
            self.assertFalse(mover.is_alive())
            moved.append(True)

        with patch(
            "netbox_nso_plugin.apply_state.lock_device_intent_transaction",
            side_effect=lock_then_move,
        ):
            response = self.client.post(
                reverse("plugins:netbox_nso_plugin:switchport_accept", args=[state.pk]),
            )

        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        interface.refresh_from_db()
        self.assertEqual(interface.device_id, other_device.pk)
        self.assertEqual(state.status, "imported")

    def test_switchport_accept_reloads_vlan_references_after_a_concurrent_rescope(self):
        import threading
        from unittest.mock import patch

        from django.db import connections, transaction
        from django.test import Client
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.models import NSOSwitchportState, NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.vlan_reconciler import rescope_vlan

        source_vlan = self.vlan_state.vlan
        interface = self._create_interface(device=self.device, name="Ethernet9.38", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            switchport_state = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=interface,
                mode="access",
                untagged_vlan=source_vlan,
                status="changed",
            )
        target_group = VLANGroup.objects.create(name="Switchport target", slug="switchport-target")
        with transaction.atomic():
            target_vlan = VLAN.objects.create(group=target_group, vid=source_vlan.vid, name=source_vlan.name)
        rescope_holds_device = threading.Event()
        release_rescope = threading.Event()
        accept_waiting = threading.Event()
        errors = []
        original_device_lock = apply_state.lock_device_intent_transaction
        original_shared_lock = apply_state.lock_shared_dependencies
        rescoping = None
        accepting = None

        def hold_rescope_device_lock(device_id):
            original_device_lock(device_id)
            if threading.current_thread() is rescoping and device_id == self.device.pk:
                rescope_holds_device.set()
                if not release_rescope.wait(10):
                    raise AssertionError("switchport Accept did not inspect the device-intent fence")

        def note_accept_waiting(keys):
            if threading.current_thread() is accepting:
                accept_waiting.set()
            return original_shared_lock(keys)

        def merge_vlan():
            try:
                with suppress_intent_push(), transaction.atomic():
                    rescope_vlan(NSOVLANState.objects.get(pk=self.vlan_state.pk), target_group)
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def accept_switchport():
            client = Client()
            client.force_login(self.user)
            try:
                with suppress_intent_push():
                    response = client.post(
                        reverse("plugins:netbox_nso_plugin:switchport_accept", args=[switchport_state.pk]),
                    )
                if response.status_code != 302:
                    raise AssertionError(f"switchport Accept returned HTTP {response.status_code}")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        with (
            patch(
                "netbox_nso_plugin.apply_state.lock_device_intent_transaction",
                side_effect=hold_rescope_device_lock,
            ),
            patch(
                "netbox_nso_plugin.apply_state.lock_shared_dependencies",
                side_effect=note_accept_waiting,
            ),
        ):
            rescoping = threading.Thread(target=merge_vlan)
            accepting = threading.Thread(target=accept_switchport)
            rescoping.start()
            self.assertTrue(rescope_holds_device.wait(10), "rescope did not acquire the device-intent fence")
            accepting.start()
            try:
                self.assertTrue(accept_waiting.wait(10), "switchport Accept did not inspect the device-intent fence")
            finally:
                release_rescope.set()
                rescoping.join(10)
                accepting.join(10)

        self.assertFalse(rescoping.is_alive())
        self.assertFalse(accepting.is_alive())
        if errors:
            raise errors[0]
        switchport_state.refresh_from_db()
        interface.refresh_from_db()
        self.assertEqual(switchport_state.status, "accepted")
        self.assertEqual(switchport_state.untagged_vlan_id, target_vlan.pk)
        self.assertEqual(interface.untagged_vlan_id, target_vlan.pk)
        self.assertFalse(VLAN.objects.filter(pk=source_vlan.pk).exists())

    def test_switchport_accept_locks_vlan_dependencies_before_the_device(self):
        import threading
        from unittest.mock import patch

        from django.db import connections, transaction
        from django.test import Client
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.models import NSOSwitchportState, NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.vlan_reconciler import rescope_vlan

        source_vlan = self.vlan_state.vlan
        interface = self._create_interface(device=self.device, name="Ethernet9.39", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            switchport_state = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=interface,
                mode="access",
                untagged_vlan=source_vlan,
                status="changed",
            )
        target_group = VLANGroup.objects.create(name="Lock-order target", slug="lock-order-target")
        with transaction.atomic():
            VLAN.objects.create(group=target_group, vid=source_vlan.vid, name=source_vlan.name)
        accept_holds_device = threading.Event()
        release_accept = threading.Event()
        rescope_holds_vlan = threading.Event()
        errors = []
        original_device_lock = apply_state.lock_device_intent_transaction
        original_shared_lock = apply_state.lock_shared_dependencies
        rescoping = None
        accepting = None

        def hold_accept_device_lock(device_id):
            original_device_lock(device_id)
            if threading.current_thread() is accepting and device_id == self.device.pk:
                accept_holds_device.set()
                if not release_accept.wait(10):
                    raise AssertionError("rescope did not inspect the VLAN-before-device lock order")

        def note_rescope_vlan_lock(keys):
            original_shared_lock(keys)
            if threading.current_thread() is rescoping and ("vlan", str(source_vlan.pk)) in keys:
                rescope_holds_vlan.set()

        def accept_switchport():
            client = Client()
            client.force_login(self.user)
            try:
                with suppress_intent_push():
                    response = client.post(
                        reverse("plugins:netbox_nso_plugin:switchport_accept", args=[switchport_state.pk]),
                    )
                if response.status_code != 302:
                    raise AssertionError(f"switchport Accept returned HTTP {response.status_code}")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def merge_vlan():
            try:
                with suppress_intent_push(), transaction.atomic():
                    rescope_vlan(NSOVLANState.objects.get(pk=self.vlan_state.pk), target_group)
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        with (
            patch(
                "netbox_nso_plugin.apply_state.lock_device_intent_transaction",
                side_effect=hold_accept_device_lock,
            ),
            patch(
                "netbox_nso_plugin.apply_state.lock_shared_dependencies",
                side_effect=note_rescope_vlan_lock,
            ),
        ):
            accepting = threading.Thread(target=accept_switchport)
            rescoping = threading.Thread(target=merge_vlan)
            accepting.start()
            self.assertTrue(accept_holds_device.wait(10), "switchport Accept did not acquire the device lock")
            rescoping.start()
            try:
                rescope_locked_vlan_while_accept_held_device = rescope_holds_vlan.wait(1)
            finally:
                release_accept.set()
                accepting.join(10)
                rescoping.join(10)

        self.assertFalse(
            rescope_locked_vlan_while_accept_held_device,
            "rescope acquired the VLAN lock after switchport Accept already held the device lock",
        )
        self.assertFalse(accepting.is_alive())
        self.assertFalse(rescoping.is_alive())
        if errors:
            raise errors[0]

    def test_vlan_vid_change_does_not_push_an_unowned_switchport_in_auto_apply_mode(self):
        from unittest.mock import patch

        from netbox_nso_plugin.models import NSOSwitchportState

        self.mgmt.auto_apply = True
        with without_commit_drain(), transaction.atomic():
            self.mgmt.save(update_fields=["auto_apply"])
        interface = self._create_interface(device=self.device, name="Ethernet9.37", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=interface,
                mode="access",
                untagged_vlan=self.vlan_state.vlan,
                status="imported",
            )

        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule, transaction.atomic():
            vlan = self.vlan_state.vlan
            vlan.vid = 2215
            vlan.save(update_fields=["vid"])

        state.refresh_from_db()
        self.assertEqual(state.status, "changed")
        self.assertNotIn(
            ((self.device.pk, "switchport"),),
            [call.args for call in schedule.call_args_list],
        )

    def test_vlan_vid_change_does_not_schedule_unowned_vlan_or_svi_intent(self):
        from unittest.mock import patch

        from netbox_nso_plugin.intent_state import footprint_for_instance, mirror_transaction
        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.signals import suppress_intent_push

        content_update(
            type(self.vlan_state).objects.get(pk=self.vlan_state.pk),
            status="imported",
        )
        interface = self._create_interface(device=self.device, name="Vlan2213-unowned", type="virtual")
        svi_state = NSOSVIState(
            management=self.mgmt,
            interface=interface,
            vlan=self.vlan_state.vlan,
            status="imported",
        )
        with suppress_intent_push(), mirror_transaction(footprint_for_instance(svi_state)):
            svi_state.save(force_insert=True)

        with patch("netbox_nso_plugin.signals._schedule_intent_push") as schedule, transaction.atomic():
            vlan = self.vlan_state.vlan
            vlan.vid = 2216
            vlan.save(update_fields=["vid"])

        self.vlan_state.refresh_from_db()
        svi_state.refresh_from_db()
        self.assertEqual((self.vlan_state.status, svi_state.status), ("changed", "changed"))
        scheduled = {args[0][1] for args, _kwargs in (entry for entry in schedule.call_args_list)}
        self.assertTrue({"vlan", "svi"}.isdisjoint(scheduled))

    def test_a_vlan_id_change_repends_every_scope_that_renders_the_vid(self):

        from netbox_nso_plugin.models import NSOSVIState, NSOSwitchportState
        from netbox_nso_plugin.views import _prepare_apply

        interface = self._create_interface(device=self.device, name="Vlan2213", type="virtual")
        switchport_interface = self._create_interface(
            device=self.device,
            name="Ethernet9.38",
            type="1000base-t",
        )
        with without_commit_drain(), transaction.atomic():
            svi_state = NSOSVIState.objects.create(
                management=self.mgmt,
                interface=interface,
                vlan=self.vlan_state.vlan,
                status="accepted",
            )
            switchport_state = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=switchport_interface,
                mode="access",
                untagged_vlan=self.vlan_state.vlan,
                status="in_sync",
            )

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        config, session = adapter.patches()
        with config, session:
            _prepare_apply(self.mgmt)

        self.vlan_state.refresh_from_db()
        svi_state.refresh_from_db()
        switchport_state.refresh_from_db()
        self.assertEqual(
            (self.vlan_state.status, svi_state.status, switchport_state.status),
            ("deploying", "deploying", "in_sync"),
        )
        with without_commit_drain(), transaction.atomic():
            vlan = self.vlan_state.vlan
            vlan.vid = 2214
            vlan.save(update_fields=["vid"])

        self.vlan_state.refresh_from_db()
        svi_state.refresh_from_db()
        switchport_state.refresh_from_db()
        self.assertEqual(
            (self.vlan_state.status, svi_state.status, switchport_state.status),
            ("accepted", "accepted", "accepted"),
        )

    def test_an_interface_rename_repends_every_deploying_scope_that_renders_its_name(self):
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import ASN, RIR, IPAddress
        from netbox_routing.models import BGPPeer, BGPRouter, BGPScope

        from netbox_nso_plugin.models import (
            NSOBFDInterfaceState,
            NSOBGPPeerState,
            NSOInterfaceIPState,
            NSOInterfaceMtuState,
            NSOInterfaceState,
            NSOISISInterfaceState,
            NSOLACPBundleState,
            NSOOSPFInterfaceState,
            NSOSubinterfaceState,
            NSOSVIState,
            NSOSwitchportState,
        )
        from netbox_nso_plugin.views import _prepare_apply

        with without_commit_drain(), transaction.atomic():
            shared = self._create_interface(device=self.device, name="Ethernet9.40", type="1000base-t")
            child = self._create_interface(
                device=self.device,
                name="Ethernet9-40-100",
                type="virtual",
                parent=shared,
            )
            rir = RIR.objects.create(name="Apply selector private ASNs", slug="apply-selector-private-asns")
            local_as = ASN.objects.create(asn=65040, rir=rir)
            remote_as = ASN.objects.create(asn=65041, rir=rir)
            router = BGPRouter.objects.create(
                assigned_object_type=ContentType.objects.get_for_model(type(self.device)),
                assigned_object_id=self.device.pk,
                asn=local_as,
                name="65040",
            )
            scope = BGPScope.objects.create(router=router)
            peer_address = IPAddress.objects.create(address="198.18.40.2/32")
            peer = BGPPeer.objects.create(
                scope=scope,
                peer=peer_address,
                remote_as=remote_as,
                update_source=shared,
                enabled=True,
            )
        bgp_state = NSOBGPPeerState.objects.get(bgp_peer=peer)
        mirror_update(bgp_state, status="in_sync")
        with without_commit_drain(), transaction.atomic():
            states = [
                NSOSVIState.objects.create(
                    management=self.mgmt,
                    interface=shared,
                    vlan=self.vlan_state.vlan,
                    status="accepted",
                ),
                NSOSubinterfaceState.objects.create(
                    management=self.mgmt,
                    interface=child,
                    parent_interface=shared,
                    dot1q_vlan=100,
                    status="accepted",
                ),
                NSOBFDInterfaceState.objects.create(
                    management=self.mgmt,
                    interface=shared,
                    min_tx=300,
                    min_rx=300,
                    multiplier=3,
                    status="accepted",
                ),
                NSOInterfaceMtuState.objects.create(
                    management=self.mgmt,
                    interface=shared,
                    l2_mtu=1600,
                    status="accepted",
                ),
                NSOInterfaceState.objects.create(
                    interface=shared,
                    attribute="description",
                    status="in_sync",
                ),
                NSOInterfaceIPState.objects.create(
                    interface=shared,
                    address="198.18.40.1/31",
                    status="in_sync",
                ),
                NSOISISInterfaceState.objects.create(
                    management=self.mgmt,
                    interface=shared,
                    af="ipv4",
                    process_tag="CORE",
                    status="in_sync",
                ),
                NSOOSPFInterfaceState.objects.create(
                    management=self.mgmt,
                    interface=shared,
                    process_id="1",
                    status="in_sync",
                ),
                NSOLACPBundleState.objects.create(
                    management=self.mgmt,
                    interface=shared,
                    lag_id=40,
                    status="in_sync",
                ),
                NSOSwitchportState.objects.create(
                    management=self.mgmt,
                    interface=shared,
                    status="in_sync",
                ),
                bgp_state,
            ]

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))
        config, session = adapter.patches()
        with config, session:
            _prepare_apply(self.mgmt)

        self.assertEqual(
            [type(state).objects.get(pk=state.pk).status for state in states],
            ["deploying"] * 4 + ["in_sync"] * 7,
        )
        with without_commit_drain(), transaction.atomic():
            shared.name = "Ethernet9.41"
            shared.save(update_fields=["name"])

        self.assertEqual(
            [type(state).objects.get(pk=state.pk).status for state in states],
            ["accepted"] * 11,
        )

    def test_overlay_intent_edit_requires_an_atomic_transaction_before_row_locking(self):

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = self._create_interface(device=self.device, name="Ethernet9.415", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="in_sync",
            )

        state.l2_mtu = 1600
        with self.assertRaisesRegex(RuntimeError, "intent_transaction requires transaction.atomic"):
            state.save(update_fields=["l2_mtu"])

        state.refresh_from_db()
        self.assertEqual(state.l2_mtu, 1500)

    def test_interface_footprint_refuses_a_move_after_the_device_lock(self):
        from unittest.mock import patch

        from dcim.models import Interface

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.intent_state import (
            IntentMutationProtocolError,
            footprint_for_instance,
            intent_transaction,
        )
        from netbox_nso_plugin.models import NSOInterfaceMtuState

        other_device, _other_mgmt = make_managed("apply-selector-interface-move", 2559)
        interface = self._create_interface(device=self.device, name="Ethernet9.416", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="in_sync",
            )
        original_lock = apply_state.lock_device_intent_transaction

        def lock_then_move(device_id):
            original_lock(device_id)
            Interface.objects.filter(pk=interface.pk).update(device=other_device)

        with self.assertRaisesRegex(IntentMutationProtocolError, "changed its renderer targets"):
            with (
                patch("netbox_nso_plugin.apply_state.lock_device_intent_transaction", side_effect=lock_then_move),
            ):
                with intent_transaction(footprint_for_instance(interface)):
                    pass

    def test_interface_rename_does_not_direct_apply_lacp_or_switchport_in_manual_mode(self):
        from unittest.mock import patch

        from netbox_nso_plugin.models import NSOLACPBundleState, NSOSwitchportState

        self.assertFalse(self.mgmt.auto_apply)
        with without_commit_drain(), transaction.atomic():
            interface = self._create_interface(device=self.device, name="Ethernet9.42", type="lag")
            lacp = NSOLACPBundleState.objects.create(
                management=self.mgmt,
                interface=interface,
                lag_id=42,
                status="in_sync",
            )
            switchport = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=interface,
                status="in_sync",
            )

        with (
            patch("netbox_nso_plugin.adapter_client.apply_lag_config") as apply_lag,
            patch("netbox_nso_plugin.adapter_client.apply_switchport_config") as apply_switchport,
            transaction.atomic(),
        ):
            interface.name = "Ethernet9.420"
            interface.save(update_fields=["name"])

        apply_lag.assert_not_called()
        apply_switchport.assert_not_called()
        lacp.refresh_from_db()
        switchport.refresh_from_db()
        self.assertEqual((lacp.status, switchport.status), ("accepted", "accepted"))

    def test_a_vlan_id_change_keeps_an_import_placeholder_out_of_the_wire_payload(self):
        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import placeholder_vlan_name

        old_vid = self.vlan_state.vlan.vid
        content_update(
            type(self.vlan_state.vlan).objects.get(pk=self.vlan_state.vlan_id),
            name=placeholder_vlan_name(old_vid),
        )
        content_update(
            NSOVLANState.objects.get(pk=self.vlan_state.pk),
            device_name="",
            status="deploying",
            apply_attempt_id=uuid4(),
        )

        with without_commit_drain(), transaction.atomic():
            vlan = type(self.vlan_state.vlan).objects.get(pk=self.vlan_state.vlan_id)
            vlan.vid = old_vid + 1
            vlan.save(update_fields=["vid"])

        self.vlan_state.refresh_from_db()
        self.vlan_state.vlan.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")
        self.assertEqual(self.vlan_state.vlan.name, placeholder_vlan_name(old_vid + 1))
        rendered = delivery.render("vlan", self.device.pk, self.mgmt.adapter_device_id)
        self.assertEqual(rendered.payload, [{"vlan_id": old_vid + 1, "name": ""}])

    def test_a_vlan_id_change_keeps_the_old_placeholder_when_the_new_name_is_taken(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.vlan_reconciler import _device_vlan_group, placeholder_vlan_name

        old_vid = self.vlan_state.vlan.vid
        new_vid = old_vid + 1
        old_placeholder = placeholder_vlan_name(old_vid)
        group = _device_vlan_group(self.device)
        content_update(self.vlan_state, device_name="")
        content_update(self.vlan_state.vlan, group=group, name=old_placeholder)
        with without_commit_drain(), transaction.atomic():
            VLAN.objects.create(
                group=group,
                vid=old_vid + 100,
                name=placeholder_vlan_name(new_vid),
            )

        with without_commit_drain(), transaction.atomic():
            vlan = VLAN.objects.get(pk=self.vlan_state.vlan_id)
            vlan.vid = new_vid
            vlan.save(update_fields=["vid"])

        vlan.refresh_from_db()
        self.assertEqual((vlan.vid, vlan.name), (new_vid, old_placeholder))

    def test_a_vlan_id_change_keeps_the_old_placeholder_when_a_qinq_sibling_has_the_new_name(self):
        from ipam.models import VLAN

        from netbox_nso_plugin.vlan_reconciler import placeholder_vlan_name

        old_vid = self.vlan_state.vlan.vid
        new_vid = old_vid + 1
        old_placeholder = placeholder_vlan_name(old_vid)
        with without_commit_drain(), transaction.atomic():
            service_vlan = VLAN.objects.create(vid=old_vid + 200, name="SERVICE", qinq_role="svlan")
        content_update(self.vlan_state, device_name="")
        content_update(
            self.vlan_state.vlan,
            group=None,
            qinq_role="cvlan",
            qinq_svlan=service_vlan,
            name=old_placeholder,
        )
        with without_commit_drain(), transaction.atomic():
            VLAN.objects.create(
                vid=old_vid + 100,
                name=placeholder_vlan_name(new_vid),
                qinq_role="cvlan",
                qinq_svlan=service_vlan,
            )

        with without_commit_drain(), transaction.atomic():
            vlan = VLAN.objects.get(pk=self.vlan_state.vlan_id)
            vlan.vid = new_vid
            vlan.save(update_fields=["vid"])

        vlan.refresh_from_db()
        self.assertEqual((vlan.vid, vlan.name), (new_vid, old_placeholder))

    def test_editing_one_deploying_row_does_not_lock_an_unrelated_row(self):
        import threading

        from django.db import connections

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        first_interface = self._create_interface(device=self.device, name="Ethernet10", type="1000base-t")
        second_interface = self._create_interface(device=self.device, name="Ethernet11", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            first = NSOInterfaceMtuState.objects.create(
                management=self.mgmt, interface=first_interface, l2_mtu=1500, status="accepted"
            )
            second = NSOInterfaceMtuState.objects.create(
                management=self.mgmt, interface=second_interface, l2_mtu=1500, status="accepted"
            )
        attempt_id = uuid4()
        mirror_update(first, status="deploying", apply_attempt_id=attempt_id)
        mirror_update(second, status="deploying", apply_attempt_id=attempt_id)

        first_locked = threading.Event()
        second_committed = threading.Event()
        release_first = threading.Event()
        errors = []

        def hold_first():
            try:
                with transaction.atomic():
                    NSOInterfaceMtuState.objects.select_for_update().get(pk=first.pk)
                    first_locked.set()
                    if not release_first.wait(10):
                        raise AssertionError("the unrelated edit waited on the first row")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        def edit_second():
            try:
                if not first_locked.wait(10):
                    raise AssertionError("the first row was not locked")
                with without_commit_drain():
                    current = NSOInterfaceMtuState.objects.get(pk=second.pk)
                    current.l2_mtu = 1600
                    _save_owned_overlay_edit(current, "interface_mtu", {"l2_mtu": 1500})
                second_committed.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        holder = threading.Thread(target=hold_first)
        editor = threading.Thread(target=edit_second)
        holder.start()
        editor.start()
        try:
            self.assertTrue(second_committed.wait(5), "the unrelated edit waited on the first row")
        finally:
            release_first.set()
        holder.join(10)
        editor.join(10)

        self.assertFalse(holder.is_alive())
        self.assertFalse(editor.is_alive())
        if errors:
            raise errors[0]
        second.refresh_from_db()
        self.assertEqual(second.status, "accepted")

    def test_promotion_does_not_render_under_its_locks(self):
        from unittest.mock import patch

        from netbox_nso_plugin import apply_state

        registry, pushed = self._promotion_snapshot()
        with patch("netbox_nso_plugin.delivery.render", side_effect=AssertionError("promotion rendered")):
            _attempt, moved = apply_state.promote_current_intent(
                self.mgmt,
                registry,
                pushed,
                apply_attempt_id=uuid4(),
                static_route_stored=False,
            )

        self.assertEqual(moved[0][2], [self.vlan_state.pk])

    def test_promotion_waits_for_an_unchanged_reconcile_transaction(self):
        import threading
        import time

        from django.db import connections

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.models import NSOVLANState

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            promotion_pid = cursor.fetchone()[0]
        registry, pushed = self._promotion_snapshot()

        row_locked = threading.Event()
        promotion_waited = threading.Event()
        abort_reconcile = threading.Event()
        errors = []

        def reconcile_without_changes():
            try:
                with transaction.atomic():
                    apply_state.lock_device_intent_transaction(self.mgmt.device_id)
                    NSOVLANState.objects.select_for_update().get(pk=self.vlan_state.pk)
                    row_locked.set()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not abort_reconcile.is_set():
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                "WHERE pid = %s AND locktype = 'advisory' AND NOT granted)",
                                [promotion_pid],
                            )
                            if cursor.fetchone()[0]:
                                promotion_waited.set()
                                return
                        abort_reconcile.wait(0.01)
                    if not abort_reconcile.is_set():
                        raise AssertionError("Apply did not wait on the reconcile transaction")
            except Exception as exc:  # noqa: BLE001 (the main test re-raises the worker failure)
                errors.append(exc)
            finally:
                connections.close_all()

        worker = threading.Thread(target=reconcile_without_changes)
        worker.start()
        try:
            self.assertTrue(row_locked.wait(10), "the reconcile did not lock the intent row")
            _attempt, moved = apply_state.promote_current_intent(
                self.mgmt,
                registry,
                pushed,
                apply_attempt_id=uuid4(),
                static_route_stored=False,
            )
        finally:
            abort_reconcile.set()
            worker.join(10)

        self.assertTrue(promotion_waited.is_set(), "Apply did not wait for the reconcile transaction")
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(moved, [(registry["vlan"].section, NSOVLANState, [self.vlan_state.pk], "accepted")])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")

    def test_promotion_stamps_the_apply_start_time(self):
        from django.utils import timezone

        from netbox_nso_plugin import apply_state

        registry, pushed = self._promotion_snapshot()
        promotion_started_at = timezone.now()
        apply_state.promote_current_intent(
            self.mgmt,
            registry,
            pushed,
            apply_attempt_id=uuid4(),
            static_route_stored=False,
        )

        self.vlan_state.refresh_from_db()
        self.assertIsNotNone(self.vlan_state.last_apply_at)
        self.assertGreaterEqual(self.vlan_state.last_apply_at, promotion_started_at)

    def test_a_terminal_snmp_preparation_failure_happens_before_any_direct_push(self):
        adapter = _ApplyContractAdapter(
            lambda selected: (202, _promoted(selected)),
            failed_intent_suffix="/snmp-intent",
        )

        response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("Nothing was applied", response.json()["message"])
        self.assertEqual(adapter.direct_requests, [])
        self.assertEqual(adapter.apply_requests, [])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_a_direct_push_failure_names_the_snapshot_already_applied(self):
        adapter = _ApplyContractAdapter(
            lambda selected: (202, _promoted(selected)),
            failed_direct_suffix="/switchport/apply",
        )

        response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "error")
        self.assertNotIn("Nothing was applied", response.json()["message"])
        self.assertIn("LACP", response.json()["message"])
        self.assertEqual(
            [url.rsplit("/devices/1558/", 1)[-1] for url in adapter.direct_requests],
            ["lag-config/apply", "switchport/apply"],
        )
        self.assertEqual(adapter.apply_requests, [])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_direct_snapshots_share_the_apply_preparation_deadline(self):
        from unittest.mock import patch

        adapter = _ApplyContractAdapter(lambda selected: (202, _promoted(selected)))

        with patch("netbox_nso_plugin.drain._send_clock", lambda: 121 if adapter.direct_requests else 0):
            response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        self.assertIn("LACP", response.json()["message"])
        self.assertIn("did not start before the preparation deadline expired", response.json()["message"])
        self.assertEqual(
            [url.rsplit("/devices/1558/", 1)[-1] for url in adapter.direct_requests],
            ["lag-config/apply"],
        )
        self.assertEqual(adapter.apply_requests, [])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_no_op_surfaces_every_skipped_reason_and_rolls_back_prepared_rows(self):
        adapter = _ApplyContractAdapter(lambda selected: (202, _no_op(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "no_op")
        self.assertEqual(set(adapter.apply_requests[0]["selected"]), _ADAPTER_STREAMS)
        for stream, reason in _no_op(adapter.apply_requests[0]["selected"])["skipped"].items():
            self.assertIn(stream, result["message"])
            self.assertIn(reason, result["message"])
        selected_receipts = list(adapter.receipts.values())
        self.assertEqual(len(selected_receipts), len(adapter.apply_requests[0]["selected"]))
        self.assertTrue(all(receipt["params"] == {"store_only": "true"} for receipt in selected_receipts))
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")
        self.assertIsNone(self.vlan_state.apply_attempt_id)
        from netbox_nso_plugin.models import NSOApplyAttempt

        attempt = NSOApplyAttempt.objects.get(pk=UUID(adapter.apply_requests[0]["apply_attempt_id"]))
        self.assertEqual(attempt.http_status, 200)
        self.assertEqual(attempt.response, _no_op(attempt.selected))

    def test_no_op_with_incomplete_skip_results_rolls_back_prepared_rows(self):
        from netbox_nso_plugin.views import NSODeviceActionView, _prepare_apply

        def incomplete_no_op(selected):
            result = _no_op(selected)
            result["skipped"].pop("vlan")
            result["generations"] = []
            return result

        adapter = _ApplyContractAdapter(lambda selected: (202, incomplete_no_op(selected)))
        config, session = adapter.patches()
        with config, session:
            prepared, selected = _prepare_apply(self.mgmt)

        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        response = NSODeviceActionView()._apply_result(
            RequestFactory().post("/"),
            self.mgmt,
            incomplete_no_op(dict(selected)),
            prepared,
            selected,
            label="Apply",
            is_ajax=True,
        )

        self.assertEqual(response.status_code, 502)
        self.assertJSONEqual(
            response.content,
            {"status": "error", "message": "Adapter returned incomplete Apply skip results."},
        )
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_no_op_with_malformed_skip_results_rolls_back_prepared_rows(self):
        from netbox_nso_plugin.views import NSODeviceActionView, _prepare_apply

        def malformed_no_op(selected):
            result = _no_op(selected)
            result["skipped"] = None
            result["generations"] = []
            return result

        adapter = _ApplyContractAdapter(lambda selected: (202, malformed_no_op(selected)))
        config, session = adapter.patches()
        with config, session:
            prepared, selected = _prepare_apply(self.mgmt)

        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        response = NSODeviceActionView()._apply_result(
            RequestFactory().post("/"),
            self.mgmt,
            malformed_no_op(dict(selected)),
            prepared,
            selected,
            label="Apply",
            is_ajax=True,
        )

        self.assertEqual(response.status_code, 502)
        self.assertJSONEqual(
            response.content,
            {"status": "error", "message": "Adapter returned invalid Apply skip results."},
        )
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_malformed_no_op_with_a_generation_keeps_prepared_rows_deploying(self):
        from netbox_nso_plugin.views import NSODeviceActionView, _prepare_apply

        def malformed_no_op(selected):
            result = _no_op(selected)
            result["skipped"] = None
            result["generations"] = [{"generation_id": 81}]
            return result

        adapter = _ApplyContractAdapter(lambda selected: (202, malformed_no_op(selected)))
        config, session = adapter.patches()
        with config, session:
            prepared, selected = _prepare_apply(self.mgmt)

        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
        response = NSODeviceActionView()._apply_result(
            RequestFactory().post("/"),
            self.mgmt,
            malformed_no_op(dict(selected)),
            prepared,
            selected,
            label="Apply",
            is_ajax=True,
        )

        self.assertEqual(response.status_code, 502)
        self.assertJSONEqual(
            response.content,
            {"status": "error", "message": "Adapter returned invalid Apply skip results."},
        )
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")

    def test_no_op_with_an_unknown_skip_reason_is_rejected_and_rolled_back(self):
        def malformed_no_op(selected):
            result = _no_op(selected)
            result["skipped"]["vlan"] = "upstream supplied text"
            return result

        response = self._post(_ApplyContractAdapter(lambda selected: (202, malformed_no_op(selected))))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["message"], "Adapter returned invalid Apply skip results.")
        self.assertNotIn("upstream supplied text", response.content.decode())
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_no_op_with_a_non_string_skip_reason_is_rejected_and_rolled_back(self):
        def malformed_no_op(selected):
            result = _no_op(selected)
            result["skipped"]["vlan"] = []
            return result

        response = self._post(_ApplyContractAdapter(lambda selected: (202, malformed_no_op(selected))))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["message"], "Adapter returned invalid Apply skip results.")
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_promoted_apply_rolls_back_a_skipped_stream_without_rolling_back_its_generation(self):
        from netbox_nso_plugin.models import NSOLoggingLevelState

        with without_commit_drain(), transaction.atomic():
            logging_state = NSOLoggingLevelState.objects.create(
                management=self.mgmt,
                console_severity="warning",
                status="accepted",
            )

        def promoted_with_skipped(selected):
            # Copied from ../nso-adapter/docs/api-contract.md, actions/apply 202 response.
            # The mixed skipped/promoted case uses ActionApplyOut and
            # ActionApplyGenerationOut from openapi_snapshot.json.
            return {
                "device_id": 1558,
                "outcome": "promoted",
                "job_id": 501,
                "selected": selected,
                "skipped": {
                    stream: "superseded" if stream == "vlan" else "no_receipt"
                    for stream in selected
                    if stream != "logging"
                },
                "generations": [
                    {
                        "generation_id": 81,
                        "seq": 4,
                        "job_id": 501,
                        "mode": "networked",
                        "source_push_seq": {"logging": selected["logging"]},
                        "stream_revisions": {"logging": 7},
                        "digest": "a" * 64,
                    }
                ],
            }

        adapter = _ApplyContractAdapter(lambda selected: (202, promoted_with_skipped(selected)))

        response = self._post(adapter)

        self.assertEqual(response.status_code, 200)
        result = response.json()
        skipped = promoted_with_skipped(adapter.apply_requests[0]["selected"])["skipped"]
        self.assertEqual(result["skipped"], skipped)
        for stream, reason in skipped.items():
            self.assertIn(stream, result["message"])
            self.assertIn(reason, result["message"])
        self.vlan_state.refresh_from_db()
        logging_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")
        self.assertEqual(logging_state.status, "deploying")

    def test_selective_rollback_uses_the_registry_section_vocabulary(self):
        from dataclasses import replace
        from unittest.mock import patch

        from netbox_nso_plugin import delivery
        from netbox_nso_plugin.models import NSOLoggingLevelState

        with without_commit_drain(), transaction.atomic():
            logging_state = NSOLoggingLevelState.objects.create(
                management=self.mgmt,
                console_severity="warning",
                status="accepted",
            )
        registry = delivery.delivery_keys()
        logging = replace(registry["logging"], section="system_logging")

        def promoted_logging(selected):
            return {
                "device_id": 1558,
                "outcome": "promoted",
                "job_id": 501,
                "selected": selected,
                "skipped": {stream: "no_receipt" for stream in selected if stream != logging.section},
                "generations": [
                    {
                        "generation_id": 81,
                        "seq": 4,
                        "job_id": 501,
                        "mode": "networked",
                        "source_push_seq": {logging.section: selected[logging.section]},
                        "stream_revisions": {logging.section: 7},
                        "digest": "a" * 64,
                    }
                ],
            }

        adapter = _ApplyContractAdapter(lambda selected: (202, promoted_logging(selected)))
        with patch.dict(registry, {"logging": logging}):
            response = self._post(adapter)

        self.assertEqual(response.status_code, 200)
        self.assertIn(logging.section, adapter.apply_requests[0]["selected"])
        self.vlan_state.refresh_from_db()
        logging_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")
        self.assertEqual(logging_state.status, "deploying")

    def test_apply_unexecutable_surfaces_each_stream_reason_and_rolls_back(self):
        def refused(_selected):
            # Copied from ../nso-adapter/docs/api-contract.md, actions/apply
            # 409 apply_unexecutable, using the ErrorEnvelope from openapi_snapshot.json.
            return (
                409,
                {
                    "error": {
                        "code": "apply_unexecutable",
                        "message": "Selected streams cannot be applied faithfully",
                        "detail": {
                            "streams": {
                                "interface_config": "interface_attribute_eligibility_unresolved",
                                "static_route": "outstanding_deletion_provenance",
                            }
                        },
                    }
                },
            )

        adapter = _ApplyContractAdapter(refused)

        response = self._post(adapter)

        self.assertEqual(response.status_code, 409)
        result = response.json()
        self.assertEqual(result["status"], "error")
        self.assertIn("interface_config", result["message"])
        self.assertIn("interface_attribute_eligibility_unresolved", result["message"])
        self.assertIn("static_route", result["message"])
        self.assertIn("outstanding_deletion_provenance", result["message"])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_apply_unexecutable_does_not_reflect_unknown_streams_or_reasons(self):
        supplied = "traceback: private upstream detail"

        def refused(_selected):
            return (
                409,
                {
                    "error": {
                        "code": "apply_unexecutable",
                        "message": "Selected streams cannot be applied faithfully",
                        "detail": {"streams": {"private_stream": supplied, "vlan": supplied}},
                    }
                },
            )

        response = self._post(_ApplyContractAdapter(refused))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], "Apply cannot execute the selected streams")
        self.assertNotIn(supplied, response.content.decode())
        self.assertNotIn("private_stream", response.content.decode())

    def test_apply_unexecutable_does_not_crash_on_a_non_string_reason(self):
        def refused(_selected):
            return (
                409,
                {
                    "error": {
                        "code": "apply_unexecutable",
                        "message": "Selected streams cannot be applied faithfully",
                        "detail": {"streams": {"vlan": {}}},
                    }
                },
            )

        response = self._post(_ApplyContractAdapter(refused))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], "Apply cannot execute the selected streams")
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_duplicate_generation_ids_keep_prepared_rows_deploying(self):
        def duplicate_generation_id(selected):
            result = _promoted(selected)
            result["generations"][1]["generation_id"] = result["generations"][0]["generation_id"]
            return result

        response = self._post(_ApplyContractAdapter(lambda selected: (202, duplicate_generation_id(selected))))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["message"], "Adapter returned an invalid Apply generation.")
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")

    def test_apply_unexecutable_with_non_mapping_detail_still_returns_conflict(self):
        def refused(_selected):
            return (
                409,
                {
                    "error": {
                        "code": "apply_unexecutable",
                        "message": "Selected streams cannot be applied faithfully",
                        "detail": ["malformed detail"],
                    }
                },
            )

        response = self._post(_ApplyContractAdapter(refused))

        self.assertEqual(response.status_code, 409)
        self.assertIn("Apply cannot execute the selected streams", response.json()["message"])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_conflict_keeps_the_existing_incumbent_job_semantics_and_rolls_back(self):
        def conflict(_selected):
            # This is the actions/apply 409 ErrorEnvelope from the adapter contract.
            return (
                409,
                {
                    "error": {
                        "code": "conflict",
                        "message": "A job is already queued or running for this device",
                        "detail": {"job_id": 900},
                    }
                },
            )

        response = self._post(_ApplyContractAdapter(conflict))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "status": "conflict",
                "message": "Another job is already queued or running: apply. (Job ID: 900)",
                "job_id": 900,
                "job_type": "apply",
            },
        )
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_the_deployment_gate_refuses_the_apply_in_the_json_the_caller_parses(self):
        """The gate is the plugin's own middleware, so the Apply view never runs.

        `IntentDeploymentMiddleware` answers the POST itself, which makes the 503 the only
        thing the tab's `runAction` ever sees for a quiesced Apply. It does `await r.json()`
        on every action response, so a text/plain body reaches the operator as a generic
        parse-error "Request failed" instead of the deliberate refusal.
        """
        from netbox_nso_plugin.deployment import quiesce, resume

        calls = []

        def unreachable(selected):
            calls.append(selected)
            return 202, _promoted(selected)

        quiesce()
        try:
            response = self._post(_ApplyContractAdapter(unreachable))
        finally:
            resume()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("deployment", response.json()["message"].lower())
        self.assertEqual(calls, [], "the gate let an Apply reach the adapter")
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_an_adapter_503_uses_the_fixed_public_message_and_keeps_deploying(self):
        """The response must not expose an upstream failure message."""

        def unavailable(_selected):
            # Copied from ErrorEnvelope in ../nso-adapter/tests/api/openapi_snapshot.json,
            # with the nso_unreachable code the adapter answers 503 with.
            return 503, {"error": {"code": "nso_unreachable", "message": "NSO is not reachable", "detail": {}}}

        response = self._post(_ApplyContractAdapter(unavailable))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["message"], "The NSO adapter request failed. See the server log.")
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")
