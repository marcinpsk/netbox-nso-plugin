# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""The manual Apply selector contract and its operator-visible outcomes."""

from __future__ import annotations

from dcim.models import Interface
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase
from django.urls import reverse
from requests.exceptions import ConnectionError

from ._adapter_http import make_response
from ._outbox_case import ReceiptAdapter, make_managed, own_vlan, without_commit_drain
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
            stream: ("superseded", "already_applied", "already_authorized", "no_receipt")[index % 4]
            for index, stream in enumerate(sorted(selected))
        },
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
            if not isinstance(body, dict) or set(body) != {"selected"} or not isinstance(body["selected"], dict):
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

    def _post(self, adapter):
        config, session = adapter.patches()
        url = reverse(
            "plugins:netbox_nso_plugin:nsodevicemanagement_action",
            args=[self.mgmt.pk, "apply"],
        )
        with config, session:
            return self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

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
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")

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
                type(self.vlan_state).objects.filter(pk=self.vlan_state.pk).update(status="accepted")
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
                type(self.vlan_state).objects.filter(pk=self.vlan_state.pk).update(status="accepted")
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
            result["skipped"] = {"logging": "unchanged"}
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
            interface = Interface.objects.create(device=self.device, name="Ethernet1", type="1000base-t")
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

    def test_no_op_with_incomplete_skip_results_rolls_back_prepared_rows(self):
        from netbox_nso_plugin.views import NSODeviceActionView, _prepare_apply

        def incomplete_no_op(selected):
            result = _no_op(selected)
            result["skipped"].pop("vlan")
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
                                "interface_config": "live_read_execution",
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
        self.assertIn("live_read_execution", result["message"])
        self.assertIn("static_route", result["message"])
        self.assertIn("outstanding_deletion_provenance", result["message"])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

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

    def test_an_adapter_503_keeps_the_adapters_own_message_and_rolls_back(self):
        """Only the plugin's middleware speaks for the gate; an adapter 503 is not it."""

        def unavailable(_selected):
            # Copied from ErrorEnvelope in ../nso-adapter/tests/api/openapi_snapshot.json,
            # with the nso_unreachable code the adapter answers 503 with.
            return 503, {"error": {"code": "nso_unreachable", "message": "NSO is not reachable", "detail": {}}}

        response = self._post(_ApplyContractAdapter(unavailable))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("NSO is not reachable", response.json()["message"])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")


class TestApplyChainSettlement(SimpleTestCase):
    """Apply remains active until every device-writing link in its chain settles."""

    def test_a_running_removal_successor_keeps_apply_active(self):
        from unittest.mock import patch

        from netbox_nso_plugin.reconcile import _apply_job_state

        jobs = [
            # Copied from JobOut in ../nso-adapter/tests/api/openapi_snapshot.json.
            {"id": 502, "type": "removal", "status": "running", "result": None},
            {"id": 501, "type": "apply", "status": "succeeded", "result": {}},
        ]

        # Empty list is the landed GET devices/{id}/generations list shape before any promotion.
        with (
            patch("netbox_nso_plugin.adapter_client.list_jobs", return_value=jobs),
            patch("netbox_nso_plugin.adapter_client.list_device_generations", return_value=[]),
        ):
            last_apply, active = _apply_job_state(1558)

        self.assertEqual(last_apply["id"], 501)
        self.assertTrue(active)
