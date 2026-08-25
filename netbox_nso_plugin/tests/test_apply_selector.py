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
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = Interface.objects.create(device=self.device, name="Ethernet9", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
            )

        state.l2_mtu = 1600
        with without_commit_drain():
            _save_owned_overlay_edit(state, "interface_mtu")

        state.refresh_from_db()
        self.assertEqual(state.status, "accepted")

    def test_a_stale_overlay_instance_cannot_restore_deploying(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = Interface.objects.create(device=self.device, name="Ethernet9.1", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
            )

        stale = NSOInterfaceMtuState.objects.get(pk=state.pk)
        current = NSOInterfaceMtuState.objects.get(pk=state.pk)
        current.l2_mtu = 1600
        with without_commit_drain():
            _save_owned_overlay_edit(current, "interface_mtu")

        stale.l2_mtu = 1700
        with without_commit_drain():
            _save_owned_overlay_edit(stale, "interface_mtu")

        stale.refresh_from_db()
        self.assertEqual(stale.status, "accepted")

    def test_a_same_value_stale_overlay_edit_cannot_restore_deploying(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = Interface.objects.create(device=self.device, name="Ethernet9.11", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
            )

        first = NSOInterfaceMtuState.objects.get(pk=state.pk)
        stale = NSOInterfaceMtuState.objects.get(pk=state.pk)
        first.l2_mtu = 1600
        with without_commit_drain():
            _save_owned_overlay_edit(first, "interface_mtu")

        stale.l2_mtu = 1600
        with without_commit_drain():
            _save_owned_overlay_edit(stale, "interface_mtu")

        stale.refresh_from_db()
        self.assertEqual(stale.status, "accepted")

    def test_a_stale_full_save_cannot_restore_deploying_without_an_intent_change(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = Interface.objects.create(device=self.device, name="Ethernet9.15", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            stale = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
            )
        NSOInterfaceMtuState.objects.filter(pk=stale.pk).update(status="accepted")

        with without_commit_drain(), transaction.atomic():
            stale.save()

        stale.refresh_from_db()
        self.assertEqual(stale.status, "accepted")

    def test_a_stale_accepted_full_save_cannot_repend_unchanged_deploying_intent(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = Interface.objects.create(device=self.device, name="Ethernet9.155", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            stale = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="accepted",
            )
        NSOInterfaceMtuState.objects.filter(pk=stale.pk).update(status="deploying")

        with without_commit_drain(), transaction.atomic():
            stale.save()

        stale.refresh_from_db()
        self.assertEqual(stale.status, "deploying")

    def test_a_wire_field_update_repends_an_in_sync_row_when_status_is_not_in_update_fields(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = Interface.objects.create(device=self.device, name="Ethernet9.16", type="1000base-t")
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
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        for index, stale_status in enumerate(("deploying", "in_sync", "apply_failed")):
            with self.subTest(status=stale_status):
                interface = Interface.objects.create(
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
                    )
                NSOInterfaceMtuState.objects.filter(pk=stale.pk).update(status="imported")

                stale.l2_mtu = 1600
                with without_commit_drain(), transaction.atomic():
                    stale.save()

                stale.refresh_from_db()
                self.assertEqual(stale.status, "changed")

    def test_a_stale_wire_only_update_persists_changed_after_unaccept(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        interface = Interface.objects.create(device=self.device, name="Ethernet9.29", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            stale = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="in_sync",
            )
        NSOInterfaceMtuState.objects.filter(pk=stale.pk).update(status="imported")

        stale.l2_mtu = 1600
        with without_commit_drain(), transaction.atomic():
            stale.save(update_fields=["l2_mtu"])

        stale.refresh_from_db()
        self.assertEqual(stale.status, "changed")

    def test_a_stale_transient_owned_status_cannot_undo_unaccept_when_intent_is_unchanged(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceMtuState

        for index, stale_status in enumerate(("deploying", "in_sync", "apply_failed")):
            with self.subTest(status=stale_status):
                interface = Interface.objects.create(
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
                    )
                NSOInterfaceMtuState.objects.filter(pk=stale.pk).update(status="imported")

                with without_commit_drain(), transaction.atomic():
                    stale.save()

                stale.refresh_from_db()
                self.assertEqual(stale.status, "imported")

    def test_suppressed_save_discards_the_previous_intent_change_verdict(self):
        from django.utils import timezone

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.signals import suppress_intent_push

        interface = Interface.objects.create(device=self.device, name="Ethernet9.35", type="1000base-t")
        with suppress_intent_push():
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

        from dcim.models import Interface
        from django.db import connections
        from django.db.models.signals import pre_save

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = Interface.objects.create(device=self.device, name="Ethernet9.2", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            state = NSOInterfaceMtuState.objects.create(
                management=self.mgmt,
                interface=interface,
                l2_mtu=1500,
                status="deploying",
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
                with transaction.atomic():
                    NSOInterfaceMtuState.objects.select_for_update().filter(pk=state.pk).update(
                        l2_mtu=1600,
                        status="accepted",
                    )
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
                    _save_owned_overlay_edit(stale, "interface_mtu")
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

        from netbox_nso_plugin.models import NSOBFDInterfaceState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.views import NSOBFDInterfaceStateAcceptView

        interface = Interface.objects.create(device=self.device, name="Ethernet9.4", type="1000base-t")
        with suppress_intent_push():
            state = NSOBFDInterfaceState.objects.create(
                management=self.mgmt,
                interface=interface,
                min_tx=300,
                min_rx=300,
                multiplier=3,
                status="imported",
            )

        row_updated = threading.Event()
        accept_waited = threading.Event()
        errors = []
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            accept_pid = cursor.fetchone()[0]

        def promote_row():
            try:
                with transaction.atomic():
                    NSOBFDInterfaceState.objects.filter(pk=state.pk).update(status="deploying")
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

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        interface = Interface.objects.create(device=self.device, name="Ethernet9.39", type="1000base-t")
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
                with transaction.atomic():
                    Interface.objects.select_for_update().get(pk=interface.pk)
                    native_locked.set()
                    if not allow_writer.wait(10):
                        raise AssertionError("the rename did not inspect the native lock")
                    current = NSOInterfaceMtuState.objects.get(pk=state.pk)
                    current.l2_mtu = 1600
                    with without_commit_drain():
                        _save_owned_overlay_edit(current, "interface_mtu")
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
        from netbox_nso_plugin.signals import suppress_intent_push

        with suppress_intent_push():
            interface = Interface.objects.create(device=self.device, name="Ethernet9.395", type="lag")
            child = Interface.objects.create(
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
            unlinked_bgp_state = NSOBGPPeerState.objects.create(
                management=self.mgmt,
                asn_str="65042",
                peer_address_str="198.18.95.2",
                remote_as_str="65043",
                status="accepted",
            )
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
                with suppress_intent_push(), transaction.atomic():
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
                with suppress_intent_push(), transaction.atomic():
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

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push

        vlan = VLAN.objects.create(vid=3558, name="before-rename")
        with suppress_intent_push():
            NSOVLANState.objects.create(management=self.mgmt, vlan=vlan, device_name=vlan.name, status="imported")
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
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push

        vlan = VLAN.objects.create(vid=3557, name="attach-before-rename")
        with suppress_intent_push():
            NSOVLANState.objects.create(management=self.mgmt, vlan=vlan, device_name=vlan.name, status="imported")
        other_device, other_mgmt = make_managed("apply-selector-attach-order", 2557)
        attach_client = Client()
        attach_client.force_login(self.user)
        attach_between_locks = threading.Event()
        release_attach = threading.Event()
        rename_holds_vlan_intent = threading.Event()
        errors = []
        original_device_membership_lock = apply_state.lock_device_vlan_membership_transaction
        original_vlan_intent_lock = apply_state.lock_vlan_intent_transaction
        attacher = None
        renamer = None

        def hold_attach_after_device_membership(device_id):
            original_device_membership_lock(device_id)
            if threading.current_thread() is attacher and device_id == other_device.pk:
                attach_between_locks.set()
                if not release_attach.wait(10):
                    raise AssertionError("the VLAN rename did not reach its intent lock")

        def note_rename_vlan_intent(vlan_id):
            original_vlan_intent_lock(vlan_id)
            if threading.current_thread() is renamer and vlan_id == vlan.pk:
                rename_holds_vlan_intent.set()

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
                if not attach_between_locks.wait(10):
                    raise AssertionError("the attachment did not pause between its locks")
                with without_commit_drain(), transaction.atomic():
                    current = VLAN.objects.get(pk=vlan.pk)
                    current.name = "rename-after-attach-started"
                    current.save(update_fields=["name"])
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                errors.append(exc)
            finally:
                connections.close_all()

        with (
            patch(
                "netbox_nso_plugin.apply_state.lock_device_vlan_membership_transaction",
                side_effect=hold_attach_after_device_membership,
            ),
            patch(
                "netbox_nso_plugin.apply_state.lock_vlan_intent_transaction",
                side_effect=note_rename_vlan_intent,
            ),
        ):
            attacher = threading.Thread(target=attach_vlan)
            renamer = threading.Thread(target=rename_vlan)
            attacher.start()
            self.assertTrue(attach_between_locks.wait(10), "the attachment did not pause between its locks")
            renamer.start()
            try:
                self.assertTrue(rename_holds_vlan_intent.wait(10), "the rename did not acquire VLAN intent")
            finally:
                release_attach.set()
                attacher.join(10)
                renamer.join(10)

        self.assertFalse(attacher.is_alive())
        self.assertFalse(renamer.is_alive())
        if errors:
            raise errors[0]
        self.assertTrue(NSOVLANState.objects.filter(management=other_mgmt, vlan=vlan).exists())
        vlan.refresh_from_db()
        self.assertEqual(vlan.name, "rename-after-attach-started")

    def test_native_vlan_prelocks_leave_malformed_payloads_to_scope_isolation(self):
        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.svi_reconciler import lock_svi_reconcile_dependencies
        from netbox_nso_plugin.vlan_reconciler import (
            lock_switchport_reconcile_dependencies,
            lock_vlan_reconcile_dependencies,
        )

        state_before = list(NSOVLANState.objects.filter(management=self.mgmt).values_list("pk", "vlan_id", "status"))
        with transaction.atomic():
            results = [
                lock_vlan_reconcile_dependencies(
                    self.device,
                    {"vlans": [{"vlan_id": "not-an-integer"}, None]},
                ),
                lock_svi_reconcile_dependencies(
                    self.device,
                    {"interfaces": [{"vlan_id": "not-an-integer"}, None]},
                ),
                lock_switchport_reconcile_dependencies(
                    self.device,
                    {"interfaces": [{"untagged_vlan": "bad", "tagged_vlans": 5}, None]},
                ),
                lock_vlan_reconcile_dependencies(self.device, {"vlans": 5}),
                lock_svi_reconcile_dependencies(self.device, {"interfaces": 5}),
                lock_switchport_reconcile_dependencies(self.device, {"interfaces": 5}),
            ]

        self.assertEqual(results, [None] * 6)
        self.assertEqual(
            list(NSOVLANState.objects.filter(management=self.mgmt).values_list("pk", "vlan_id", "status")),
            state_before,
        )

    def test_advisory_lock_helpers_use_distinct_transaction_namespaces(self):
        from netbox_nso_plugin.apply_state import (
            lock_device_intent_transaction,
            lock_device_vlan_membership_transaction,
            lock_vlan_intent_transaction,
            lock_vlan_membership_transaction,
            lock_vlan_rescope_transaction,
        )

        lock_id = 1611
        namespaces = [
            1_503_003_007,
            1_503_003_008,
            1_503_003_009,
            1_503_003_010,
            1_503_003_011,
        ]
        helpers = [
            lock_device_intent_transaction,
            lock_vlan_intent_transaction,
            lock_device_vlan_membership_transaction,
            lock_vlan_membership_transaction,
            lock_vlan_rescope_transaction,
        ]

        with transaction.atomic():
            for helper in helpers:
                helper(lock_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT classid::bigint, objid::bigint, objsubid, mode, granted "
                    "FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory' "
                    "AND classid = ANY(%s) AND objid = %s ORDER BY classid",
                    [namespaces, lock_id],
                )
                held_locks = cursor.fetchall()

        self.assertEqual(
            held_locks,
            [(namespace, lock_id, 2, "ExclusiveLock", True) for namespace in namespaces],
        )
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

        from netbox_nso_plugin.models import NSOVLANState
        from netbox_nso_plugin.vlan_reconciler import lock_switchport_reconcile_dependencies

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
                with transaction.atomic():
                    lock_switchport_reconcile_dependencies(
                        self.device,
                        {"interfaces": [{"untagged_vlan": vlan.vid, "tagged_vlans": []}]},
                    )
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

        from dcim.models import Interface
        from django.db import connections
        from django.db.models.signals import pre_save
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin.models import NSOSVIState
        from netbox_nso_plugin.vlan_reconciler import (
            lock_switchport_reconcile_dependencies,
            rescope_vlan,
        )

        target_group = VLANGroup.objects.create(name="Membership fence target", slug="membership-fence-target")
        target_vlan = VLAN.objects.create(
            group=target_group,
            vid=self.vlan_state.vlan.vid,
            name="shared-target",
        )
        svi_interface = Interface.objects.create(device=self.device, name="Vlan3559", type="virtual")
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
                with transaction.atomic():
                    lock_switchport_reconcile_dependencies(
                        self.device,
                        {"interfaces": [{"untagged_vlan": self.vlan_state.vlan.vid, "tagged_vlans": []}]},
                    )
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

    def test_vlan_rescope_move_reports_a_concurrent_target_creation_as_a_conflict(self):
        import threading
        from unittest.mock import patch

        from django.db import IntegrityError, connections
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.vlan_reconciler import VLANRescopeConflict, rescope_vlan

        target_group = VLANGroup.objects.create(name="Concurrent target", slug="concurrent-target")
        source_vlan_id = self.vlan_state.vlan_id
        source_vid = self.vlan_state.vlan.vid
        rescope_admitted = threading.Event()
        release_rescope = threading.Event()
        target_created = threading.Event()
        rescope_conflicted = threading.Event()
        rescope_errors = []
        creator_errors = []
        original_lock = apply_state.lock_vlan_membership_transaction

        def hold_after_target_lookup(vlan_id):
            original_lock(vlan_id)
            if vlan_id == source_vlan_id:
                rescope_admitted.set()
                if not release_rescope.wait(10):
                    raise AssertionError("the concurrent VLAN creator did not inspect the target-group fence")

        def move_vlan():
            try:
                rescope_vlan(type(self.vlan_state).objects.get(pk=self.vlan_state.pk), target_group)
            except VLANRescopeConflict:
                rescope_conflicted.set()
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                rescope_errors.append(exc)
            finally:
                connections.close_all()

        def create_target_vlan():
            try:
                if not rescope_admitted.wait(10):
                    raise AssertionError("the rescope did not reach its target lookup fence")
                VLAN.objects.create(group=target_group, vid=source_vid, name="concurrent")
                target_created.set()
            except IntegrityError as exc:
                creator_errors.append(exc)
            except Exception as exc:  # noqa: BLE001 (the main test re-raises worker failures)
                creator_errors.append(exc)
            finally:
                connections.close_all()

        with patch(
            "netbox_nso_plugin.apply_state.lock_vlan_membership_transaction",
            side_effect=hold_after_target_lookup,
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

        self.assertTrue(created_before_rescope, "the concurrent target VLAN did not commit")
        if rescope_errors:
            raise rescope_errors[0]
        if creator_errors:
            raise creator_errors[0]
        self.assertTrue(rescope_conflicted.is_set(), "the rescope did not report a controlled conflict")
        self.assertFalse(rescoping.is_alive())
        self.assertFalse(creator.is_alive())
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.vlan_id, source_vlan_id)
        self.assertNotEqual(self.vlan_state.vlan.group_id, target_group.pk)
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
        target_vlan = VLAN.objects.create(group=target_group, vid=source_vlan.vid, name=source_vlan.name)
        NSOVLANState.objects.filter(pk=self.vlan_state.pk).update(status="imported")
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

    def test_switchport_accept_reloads_vlan_references_after_a_concurrent_rescope(self):
        import threading
        from unittest.mock import patch

        from dcim.models import Interface
        from django.db import connections, transaction
        from django.test import Client
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.models import NSOSwitchportState, NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.vlan_reconciler import rescope_vlan

        source_vlan = self.vlan_state.vlan
        interface = Interface.objects.create(device=self.device, name="Ethernet9.38", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            switchport_state = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=interface,
                mode="access",
                untagged_vlan=source_vlan,
                status="changed",
            )
        target_group = VLANGroup.objects.create(name="Switchport target", slug="switchport-target")
        target_vlan = VLAN.objects.create(group=target_group, vid=source_vlan.vid, name=source_vlan.name)
        rescope_holds_device = threading.Event()
        release_rescope = threading.Event()
        accept_waiting = threading.Event()
        errors = []
        original_device_lock = apply_state.lock_device_intent_transaction
        original_membership_lock = apply_state.lock_device_vlan_membership_transaction
        rescoping = None
        accepting = None

        def hold_rescope_device_lock(device_id):
            original_device_lock(device_id)
            if threading.current_thread() is rescoping and device_id == self.device.pk:
                rescope_holds_device.set()
                if not release_rescope.wait(10):
                    raise AssertionError("switchport Accept did not inspect the device-intent fence")

        def note_accept_waiting(device_id):
            if threading.current_thread() is accepting and device_id == self.device.pk:
                accept_waiting.set()
            return original_membership_lock(device_id)

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
                "netbox_nso_plugin.apply_state.lock_device_vlan_membership_transaction",
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

        from dcim.models import Interface
        from django.db import connections, transaction
        from django.test import Client
        from ipam.models import VLAN, VLANGroup

        from netbox_nso_plugin import apply_state
        from netbox_nso_plugin.models import NSOSwitchportState, NSOVLANState
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.vlan_reconciler import rescope_vlan

        source_vlan = self.vlan_state.vlan
        interface = Interface.objects.create(device=self.device, name="Ethernet9.39", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            switchport_state = NSOSwitchportState.objects.create(
                management=self.mgmt,
                interface=interface,
                mode="access",
                untagged_vlan=source_vlan,
                status="changed",
            )
        target_group = VLANGroup.objects.create(name="Lock-order target", slug="lock-order-target")
        VLAN.objects.create(group=target_group, vid=source_vlan.vid, name=source_vlan.name)
        accept_holds_device = threading.Event()
        release_accept = threading.Event()
        rescope_holds_vlan = threading.Event()
        errors = []
        original_device_lock = apply_state.lock_device_intent_transaction
        original_vlan_lock = apply_state.lock_vlan_intent_transaction
        rescoping = None
        accepting = None

        def hold_accept_device_lock(device_id):
            original_device_lock(device_id)
            if threading.current_thread() is accepting and device_id == self.device.pk:
                accept_holds_device.set()
                if not release_accept.wait(10):
                    raise AssertionError("rescope did not inspect the VLAN-before-device lock order")

        def note_rescope_vlan_lock(vlan_id):
            original_vlan_lock(vlan_id)
            if threading.current_thread() is rescoping and vlan_id == source_vlan.pk:
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
                "netbox_nso_plugin.apply_state.lock_vlan_intent_transaction",
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

        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOSwitchportState

        type(self.mgmt).objects.filter(pk=self.mgmt.pk).update(auto_apply=True)
        self.mgmt.refresh_from_db()
        interface = Interface.objects.create(device=self.device, name="Ethernet9.37", type="1000base-t")
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

    def test_a_vlan_id_change_repends_every_scope_that_renders_the_vid(self):
        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOSVIState, NSOSwitchportState
        from netbox_nso_plugin.views import _prepare_apply

        interface = Interface.objects.create(device=self.device, name="Vlan2213", type="virtual")
        switchport_interface = Interface.objects.create(
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
        from dcim.models import Interface
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
        from netbox_nso_plugin.signals import suppress_intent_push
        from netbox_nso_plugin.views import _prepare_apply

        with suppress_intent_push():
            shared = Interface.objects.create(device=self.device, name="Ethernet9.40", type="1000base-t")
            child = Interface.objects.create(
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
                NSOBGPPeerState.objects.create(
                    management=self.mgmt,
                    asn_str="65040",
                    peer_address_str="198.18.40.2",
                    bgp_peer=peer,
                    remote_as_str="65041",
                    status="in_sync",
                ),
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

    def test_interface_rename_does_not_direct_apply_lacp_or_switchport_in_manual_mode(self):
        from unittest.mock import patch

        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOLACPBundleState, NSOSwitchportState
        from netbox_nso_plugin.signals import suppress_intent_push

        self.assertFalse(self.mgmt.auto_apply)
        with suppress_intent_push():
            interface = Interface.objects.create(device=self.device, name="Ethernet9.42", type="lag")
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
        NSOVLANState.objects.filter(pk=self.vlan_state.pk).update(device_name="", status="deploying")
        type(self.vlan_state.vlan).objects.filter(pk=self.vlan_state.vlan_id).update(
            name=placeholder_vlan_name(old_vid)
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

    def test_editing_one_deploying_row_does_not_lock_an_unrelated_row(self):
        import threading

        from dcim.models import Interface
        from django.db import connections

        from netbox_nso_plugin.models import NSOInterfaceMtuState
        from netbox_nso_plugin.views import _save_owned_overlay_edit

        first_interface = Interface.objects.create(device=self.device, name="Ethernet10", type="1000base-t")
        second_interface = Interface.objects.create(device=self.device, name="Ethernet11", type="1000base-t")
        with without_commit_drain(), transaction.atomic():
            first = NSOInterfaceMtuState.objects.create(
                management=self.mgmt, interface=first_interface, l2_mtu=1500, status="accepted"
            )
            second = NSOInterfaceMtuState.objects.create(
                management=self.mgmt, interface=second_interface, l2_mtu=1500, status="accepted"
            )
        NSOInterfaceMtuState.objects.filter(pk__in=(first.pk, second.pk)).update(status="deploying")
        first.status = "deploying"
        second.status = "deploying"

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
                    _save_owned_overlay_edit(current, "interface_mtu")
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

    def test_promotion_refuses_a_row_locked_by_an_intent_transaction(self):
        """Apply must fail closed when it cannot lock every candidate intent row."""
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch

        from django.db import connections

        from netbox_nso_plugin import apply_state, delivery
        from netbox_nso_plugin.models import NSOVLANState

        row_locked = threading.Event()
        release_mutation = threading.Event()
        errors = []

        def mutate():
            try:
                with without_commit_drain(), transaction.atomic():
                    NSOVLANState.objects.select_for_update().get(pk=self.vlan_state.pk)
                    row_locked.set()
                    if not release_mutation.wait(10):
                        raise AssertionError("Apply did not inspect the locked intent row")
                    vlan = self.vlan_state.vlan
                    vlan.name = "intent-changing-under-row-lock"
                    vlan.save(update_fields=["name"])
            except Exception as exc:  # noqa: BLE001 (the main test re-raises the worker failure)
                errors.append(exc)
            finally:
                connections.close_all()

        with patch("netbox_nso_plugin.apply_state._current_identity", return_value="matching-intent"):
            worker = threading.Thread(target=mutate)
            worker.start()
            self.assertTrue(row_locked.wait(10), "the mutation did not lock the intent row")
            with self.assertRaises(apply_state.IntentChangedDuringPreparation):
                apply_state.promote_current_intent(
                    self.mgmt,
                    delivery.delivery_keys(),
                    {"vlan": SimpleNamespace(identity="matching-intent")},
                    static_route_stored=False,
                )
            release_mutation.set()
            worker.join(10)

        self.assertFalse(worker.is_alive(), "the intent transaction deadlocked with Apply promotion")
        if errors:
            raise errors[0]
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "accepted")

    def test_promotion_waits_for_an_unchanged_reconcile_transaction(self):
        import threading
        import time
        from types import SimpleNamespace

        from django.db import connections

        from netbox_nso_plugin import apply_state, delivery
        from netbox_nso_plugin.models import NSOVLANState

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            promotion_pid = cursor.fetchone()[0]
        registry = delivery.delivery_keys()
        identity = apply_state._current_identity(self.mgmt, registry, "vlan")

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
            moved = apply_state.promote_current_intent(
                self.mgmt,
                registry,
                {"vlan": SimpleNamespace(identity=identity)},
                static_route_stored=False,
            )
        finally:
            abort_reconcile.set()
            worker.join(10)

        self.assertTrue(promotion_waited.is_set(), "Apply did not wait for the reconcile transaction")
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(moved, [(registry["vlan"].section, NSOVLANState, [self.vlan_state.pk])])
        self.vlan_state.refresh_from_db()
        self.assertEqual(self.vlan_state.status, "deploying")

    def test_promotion_stamps_the_apply_start_time(self):
        from types import SimpleNamespace

        from django.utils import timezone

        from netbox_nso_plugin import apply_state, delivery

        registry = delivery.delivery_keys()
        identity = apply_state._current_identity(self.mgmt, registry, "vlan")
        promotion_started_at = timezone.now()
        apply_state.promote_current_intent(
            self.mgmt,
            registry,
            {"vlan": SimpleNamespace(identity=identity)},
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
