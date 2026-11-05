# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba
"""Manual Apply admission through the adapter HTTP boundary."""

import re
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.middleware.csrf import get_token
from django.template import Context, Template
from django.test import Client, RequestFactory, SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse

from netbox_nso_plugin import adapter_client
from netbox_nso_plugin.apply_preflight import validate_preflight

from ._adapter_http import make_apply_preflight as preflight_wire
from ._adapter_http import make_response, make_session
from ._outbox_case import CFG, ReceiptAdapter, make_managed, own_vlan, without_commit_drain
from ._settlement_adapter import FakeAdapter, LoopbackOnlySession
from .mixins import IntentPushResetMixin, _CascadeFlushMixin
from .test_apply_selector import _promoted


class TestPreflightClient(SimpleTestCase):
    def test_stored_selection_preflight_posts_exact_selection(self):
        selected = {"vlan": 41}
        wire = preflight_wire(selected)
        session = make_session(json_data=wire)
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        ):
            self.assertEqual(adapter_client.apply_preflight(1558, selected), wire)
        args, kwargs = session.request.call_args
        self.assertEqual(args, ("POST", f"{CFG['url']}/api/v1/devices/1558/actions/apply-preflight"))
        self.assertEqual(kwargs["json"], {"selected": selected})

    def test_ready_admission_requires_a_clean_pass_after_dropping_a_stream(self):
        selected = {"vlan": 41, "ip": 42}
        wire = preflight_wire(selected, blocked={"ip": {"status": "blocked", "guard": {"interfaces": [["me0"]]}}})
        wire["attempts"].pop()
        session = make_session(json_data=wire)
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
            self.assertRaises(adapter_client.AdapterError) as raised,
        ):
            adapter_client.apply_preflight(1558, selected)
        self.assertEqual(raised.exception.code, "invalid_response")

    def test_malformed_or_inconsistent_admission_fails_closed(self):
        from copy import deepcopy

        good = preflight_wire({"vlan": 41})
        malformed = [None, [], {}, {**good, "surprise": True}]
        malformed.extend({key: value for key, value in good.items() if key != field} for field in good)
        malformed.extend(
            {**good, **override}
            for override in [
                {"status": "unknown"},
                {"streams": {}},
                {"streams": {"vlan": {"status": "unknown"}}},
                {"streams": {"vlan": {"status": "blocked"}}},
                {"streams": {"vlan": {"status": "admitted", "refusals": [{}]}}},
                {"admitted_selection": {"vlan": True}},
                {"admitted_selection": {"vlan": 42}},
                {"admitted_selection": {"ip": 41}},
                {"preflight_identity": None},
                {"preflight_identity": "A" * 64},
                {"skips": {"vlan": "unknown"}},
                {"baseline_refused": ["vlan"]},
                {"cross_stream": ["vlan"]},
                {"context_disturbed": ["vlan"]},
                {"baseline_refused": "vlan"},
                {"diffs": {"native": 1, "cli": ""}},
                {"refusals": [{}]},
                {"status": "error"},
                {"error": "invalid"},
                {"attempts": [{"selection": {"vlan": 42}, "dropped": []}]},
                {"attempts": []},
                {"attempts": [{"selection": {"vlan": 41}, "dropped": ["vlan"]}]},
            ]
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                session = make_session(json_data=deepcopy(payload))
                with (
                    patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=CFG),
                    patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
                    self.assertRaises(adapter_client.AdapterError) as raised,
                ):
                    adapter_client.apply_preflight(1558, {"vlan": 41})
                self.assertEqual(raised.exception.code, "invalid_response")

    def test_a_verdict_whose_status_disagrees_with_its_reasons_fails_closed(self):
        good = preflight_wire({"vlan": 41})
        cases = {
            "admitted with a reason": {
                **good,
                "streams": {"vlan": {"status": "admitted", "guard": {"interfaces": [["me0"]]}}},
            },
            "blocked without a reason": {
                **good,
                "streams": {"vlan": {"status": "blocked"}},
                "admitted_selection": {},
                "attempts": [{"selection": {"vlan": 41}, "dropped": ["vlan"]}, {"selection": {}, "dropped": []}],
            },
        }
        for name, payload in cases.items():
            with self.subTest(name):
                with self.assertRaises(adapter_client.AdapterError) as raised:
                    validate_preflight(payload, {"vlan": 41})
                self.assertEqual(raised.exception.code, "invalid_response")
                self.assertEqual(str(raised.exception.__cause__), "stream verdict reason mismatch")


class PreflightAdapter(ReceiptAdapter):
    """Serve stored selection preflight and exact identity-bound Apply."""

    def __init__(self, verdict=None):
        super().__init__()
        self.verdict = verdict or preflight_wire
        self.preflights = []
        self.identities = []
        self.applies = []
        self.stale = False
        self.apply_response = _promoted

    def _handle(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/actions/apply-preflight"):
            self.preflights.append(kwargs["json"]["selected"])
            result = self.verdict(kwargs["json"]["selected"])
            self.identities.append(result.get("preflight_identity"))
            return make_response(200, result)
        if method == "POST" and url.endswith("/actions/apply"):
            body = kwargs["json"]
            assert set(body) == {"selected", "apply_attempt_id", "preflight_identity"}
            assert body["preflight_identity"] in self.identities
            self.applies.append(body)
            if self.stale:
                return make_response(
                    409,
                    {
                        "error": {
                            "code": "preflight_stale",
                            "message": "Manual Apply needs a fresh preflight",
                            "detail": {"reason": "inputs_changed"},
                        }
                    },
                )
            return make_response(202, self.apply_response(body["selected"]))
        return super()._handle(method, url, **kwargs)


class ReplayAdmissionAdapter(PreflightAdapter):
    """Retain admission when transport or authentication hides its answer."""

    def __init__(self, *, lose_response=False):
        super().__init__()
        self.lose_response = lose_response
        self.unauthorized = False
        self.admission = None

    def _handle(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/actions/apply"):
            if self.unauthorized:
                self.applies.append(kwargs["json"])
                return make_response(401, {"error": {"code": "unauthorized", "message": "Placeholder rejection"}})
            response = super()._handle(method, url, **kwargs)
            self.admission = response.json()
            if self.lose_response:
                from requests.exceptions import ConnectionError

                self.lose_response = False
                raise ConnectionError("The admission response was lost.")
            return response
        return super()._handle(method, url, **kwargs)


class TestApplyPreflightFlow(_CascadeFlushMixin, IntentPushResetMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser("preflight-operator", password="placeholder-password")
        self.client.force_login(self.user)
        self.device, self.mgmt = make_managed("preflight-placeholder", 1558)
        self.row = own_vlan(self.mgmt, 1788, "preflight-placeholder")
        self.preparation_revisions = {}
        self.preview_url = reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk])
        self.apply_url = reverse("plugins:netbox_nso_plugin:nsodevicemanagement_action", args=[self.mgmt.pk, "apply"])

    def preview(self, adapter):
        adapter.preparation_revisions = self.preparation_revisions
        config, session = adapter.patches()
        with config, session:
            response = self.client.post(self.preview_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def confirm(self, adapter, token):
        config, session = adapter.patches()
        with config, session:
            return self.client.post(self.apply_url, {"apply_preview": token}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def assert_nothing_sent(self):
        from django.db.models import Q

        from netbox_nso_plugin.models import NSOApplyAttempt

        sent = NSOApplyAttempt.objects.filter(management=self.mgmt).filter(
            Q(send_seq__gt=0) | Q(adapter_apply_attempt_id__isnull=False)
        )
        self.assertFalse(sent.exists())

    def test_preview_prepares_before_preflight_and_confirm_reuses_it_without_preparation(self):
        adapter = PreflightAdapter()
        preview = self.preview(adapter)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")
        self.assertIsNone(self.row.apply_attempt_id)
        self.assertEqual(preview["streams"]["vlan"]["status"], "admitted")
        selected = dict(adapter.preflights[0])
        prepared_requests = list(adapter.requests)
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(adapter.applies[0]["selected"], selected)
        self.assertEqual(adapter.requests, prepared_requests)
        self.assertEqual(len(adapter.preflights), 1)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")

    @override_settings(CSRF_COOKIE_HTTPONLY=True)
    def test_preview_accepts_the_rendered_form_token_with_csrf_checks(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        request = RequestFactory().get(reverse("dcim:device_nso", args=[self.device.pk]))
        form = Template('<form method="post">{% csrf_token %}</form>').render(
            Context({"csrf_token": get_token(request)})
        )
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', form).group(1)
        client.cookies[settings.CSRF_COOKIE_NAME] = request.META["CSRF_COOKIE"]
        client.cookies[settings.CSRF_COOKIE_NAME]["httponly"] = True
        self.assertEqual(client.post(self.preview_url).status_code, 403)
        adapter = PreflightAdapter()
        config, session = adapter.patches()
        with config, session:
            response = client.post(self.preview_url, HTTP_X_CSRFTOKEN=token, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["can_apply"])
        self.assertEqual(len(adapter.preflights), 1)

    def test_confirmation_keeps_the_exact_skipped_selection(self):
        def verdict(selected):
            result = preflight_wire(selected)
            result["streams"]["snmp"] = {"status": "not_evaluated"}
            del result["admitted_selection"]["snmp"]
            result["skips"] = {"snmp": "no_receipt"}
            return result

        def admission(selected):
            result = _promoted(selected)
            result["skipped"] = {"snmp": "no_receipt"}
            for generation in result["generations"]:
                for key in ("source_push_seq", "stream_revisions"):
                    del generation[key]["snmp"]
            return result

        adapter = PreflightAdapter(verdict)
        adapter.apply_response = admission
        preview = self.preview(adapter)
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(adapter.applies[0]["selected"], adapter.preflights[0])

    def test_preview_labels_each_stream_and_names_its_skip_reason(self):
        def verdict(selected):
            result = preflight_wire(selected)
            result["streams"]["snmp"] = {"status": "not_evaluated"}
            del result["admitted_selection"]["snmp"]
            result["skips"] = {"snmp": "already_applied"}
            return result

        streams = self.preview(PreflightAdapter(verdict))["streams"]
        self.assertEqual((streams["vlan"]["label"], streams["vlan"]["skip"]), ("VLAN", ""))
        self.assertEqual((streams["snmp"]["label"], streams["snmp"]["skip"]), ("SNMP", "already applied"))

    def test_a_native_edit_after_preview_requires_new_admission(self):
        from ._outbox_case import rename_vlan

        adapter = PreflightAdapter()
        preview = self.preview(adapter)
        with without_commit_drain():
            rename_vlan(self.row, "changed placeholder")
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "preflight_stale")
        self.assertEqual(adapter.applies, [])

    def test_a_stored_preflight_that_no_longer_validates_requires_new_admission(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        adapter = PreflightAdapter()
        preview = self.preview(adapter)
        attempt = NSOApplyAttempt.objects.get(management=self.mgmt, preflight__isnull=False)
        stored = attempt.preflight
        # A stream that the delivery registry no longer knows makes the stored preflight invalid.
        NSOApplyAttempt.objects.filter(pk=attempt.pk).update(
            preflight={**stored, "selected": {**stored["selected"], "retired-stream": 1}}
        )
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["status"], "preflight_stale")
        self.assertEqual(adapter.applies, [])
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")

    def test_confirm_without_preview_does_not_prepare_or_apply(self):
        adapter = PreflightAdapter()
        response = self.confirm(adapter, "")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(adapter.requests, [])
        self.assertEqual(adapter.applies, [])

    def test_a_stale_confirm_without_javascript_does_not_reopen_the_preview_from_the_url(self):
        adapter = PreflightAdapter()
        config, session = adapter.patches()
        with config, session:
            response = self.client.post(self.apply_url, {"apply_preview": ""})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dcim:device_nso", kwargs={"pk": self.device.pk}))
        self.assertEqual(adapter.requests, [])

    def test_preview_refuses_an_active_receipt_claim_without_promoting_rows(self):
        from netbox_nso_plugin import delivery, drain
        from netbox_nso_plugin.models import NSOIntentOutboxState

        for scope in ("vlan", "snmp"):
            with self.subTest(scope=scope), without_commit_drain():
                held = drain.claim(self.device.pk, scope, mode=delivery.MODE_STORE_ONLY, force=True)
                self.assertIsNotNone(held)
                adapter = PreflightAdapter()
                config, session = adapter.patches()
                try:
                    with config, session:
                        response = self.client.post(self.preview_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                    self.assertEqual(response.status_code, 409, response.content)
                    self.assertEqual(response.json()["status"], "error")
                    self.assertIn("intent preparation failed", response.json()["message"])
                    self.assertEqual(adapter.preflights, [])
                    self.assert_nothing_sent()
                    state = NSOIntentOutboxState.objects.get(device=self.device, scope=scope)
                    self.assertEqual(state.push_seq, held.push_seq)
                    self.assertIsNotNone(state.claimed_at)
                    self.row.refresh_from_db()
                    self.assertEqual(self.row.status, "accepted")
                    self.assertIsNone(self.row.apply_attempt_id)
                finally:
                    drain.abandon(held)

    def test_preview_refuses_a_database_failure_while_storing_the_preflight(self):
        from django.db import OperationalError, connection

        adapter = PreflightAdapter()

        def lock_timeout_after_preflight(execute, sql, params, many, context):
            if adapter.preflights and "FOR UPDATE" in sql:
                raise OperationalError("canceling statement due to lock timeout")
            return execute(sql, params, many, context)

        config, session = adapter.patches()
        with config, session, connection.execute_wrapper(lock_timeout_after_preflight):
            response = self.client.post(self.preview_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("could not store the preflight", response.json()["message"])
        self.assertEqual(len(adapter.preflights), 1)
        self.assert_nothing_sent()
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")
        self.assertIsNone(self.row.apply_attempt_id)

    def test_preview_refuses_an_audit_timeout_between_preparation_calls(self):
        import time

        from netbox_nso_plugin import delivery, drain

        registry = delivery.delivery_keys()
        protocol_count = sum(entry.in_protocol for entry in registry.values())
        for prepared_count in (1, protocol_count - 1, len(registry) - 1):
            with self.subTest(prepared_count=prepared_count):
                adapter = PreflightAdapter()

                def audit_clock():
                    elapsed = 2 * drain.SEND_DEADLINE.total_seconds() if len(adapter.requests) >= prepared_count else 0
                    return time.monotonic() + elapsed

                config, session = adapter.patches()
                with config, session, patch("netbox_nso_plugin.renderer_audit._monotonic", side_effect=audit_clock):
                    response = self.client.post(self.preview_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json()["status"], "error")
                self.assertIn("preparation failed", response.json()["message"])
                self.assertEqual(len(adapter.requests), prepared_count)
                self.assertEqual(adapter.preflights, [])
                self.assert_nothing_sent()
                self.row.refresh_from_db()
                self.assertEqual(self.row.status, "accepted")
                self.assertIsNone(self.row.apply_attempt_id)

    def test_a_failed_send_claim_rolls_back_the_confirmed_promotion(self):
        from django.db import OperationalError, connection

        from netbox_nso_plugin.apply_settlement import deploying_attempt_ids
        from netbox_nso_plugin.models import NSOApplyAttempt

        adapter = PreflightAdapter()
        preview = self.preview(adapter)

        def fail_the_send_claim(execute, sql, params, many, context):
            if sql.startswith("UPDATE") and '"send_seq"' in sql:
                raise OperationalError("canceling statement due to lock timeout")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail_the_send_claim):
            response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("could not mark the selected intent as deploying", response.json()["message"])
        self.assertEqual(adapter.applies, [])
        attempt = NSOApplyAttempt.objects.get(management=self.mgmt, trigger_kind="manual")
        self.assertIsNone(attempt.adapter_apply_attempt_id)
        self.assertEqual(attempt.send_seq, 0)
        self.assertEqual(deploying_attempt_ids(self.mgmt), ())
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")

        retried = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(retried.status_code, 200, retried.content)
        self.assertEqual(len(adapter.applies), 1)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")

    def test_a_promoted_answer_without_a_chain_is_kept_and_stalls_settlement(self):
        from netbox_nso_plugin.apply_settlement import EvidenceInvariantError, _admission_keep_streams
        from netbox_nso_plugin.models import NSOApplyAttempt

        adapter = PreflightAdapter()
        adapter.apply_response = lambda selected: {**_promoted(selected), "generations": []}
        preview = self.preview(adapter)
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 502, response.content)
        self.assertIn("without a generation chain", response.json()["message"])
        attempt = NSOApplyAttempt.objects.get(pk=adapter.applies[0]["apply_attempt_id"])
        # The adapter admitted the selection, so its exact answer stays durable and the rows stay deploying.
        self.assertEqual((attempt.http_status, attempt.response["generations"]), (202, []))
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")
        with self.assertRaisesRegex(EvidenceInvariantError, "no valid generation chain"):
            _admission_keep_streams(attempt, attempt.response)

    def test_stale_identity_requires_new_preview_and_new_attempt(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        adapter = PreflightAdapter()
        preview = self.preview(adapter)
        adapter.stale = True
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["status"], "preflight_stale")
        self.assertEqual(response.json()["preview_url"], self.preview_url)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")
        stale = NSOApplyAttempt.objects.get(pk=adapter.applies[0]["apply_attempt_id"])
        self.assertEqual(stale.http_status, 409)
        self.assertEqual(set(stale.overlay_snapshots.values_list("settlement_disposition", flat=True)), {"not_applied"})

        adapter.stale = False
        fresh = self.preview(adapter)
        response = self.confirm(adapter, fresh["apply_preview"])
        self.assertEqual(response.status_code, 200, response.content)
        first, second = adapter.applies
        self.assertNotEqual(second["apply_attempt_id"], first["apply_attempt_id"])
        self.assertEqual([first["preflight_identity"], second["preflight_identity"]], adapter.identities)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")
        self.assertEqual(str(self.row.apply_attempt_id), second["apply_attempt_id"])

    def test_each_preview_confirms_with_the_identity_of_its_own_preflight(self):
        adapter = PreflightAdapter()
        older = self.preview(adapter)
        self.preview(adapter)
        self.assertNotEqual(*adapter.identities)
        response = self.confirm(adapter, older["apply_preview"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([body["preflight_identity"] for body in adapter.applies], adapter.identities[:1])

    def test_preview_refuses_a_send_deadline_that_expires_before_the_drain_audit(self):
        adapter = PreflightAdapter()
        clock = iter((0, 0, 0, 0, 241))
        config, session = adapter.patches()
        with config, session, patch("netbox_nso_plugin.drain._send_clock", side_effect=lambda: next(clock, 241)):
            response = self.client.post(self.preview_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("intent preparation failed", response.json()["message"])
        self.assertEqual(adapter.requests, [])
        self.assertEqual(adapter.preflights, [])
        self.assert_nothing_sent()
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")
        self.assertIsNone(self.row.apply_attempt_id)

    def test_preview_refuses_a_deployment_transition_started_during_preparation(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        from django.db import connection, connections

        from netbox_nso_plugin import deployment

        started = threading.Event()
        transitions = []

        def transition():
            try:
                started.set()
                with deployment.exclusive_transition():
                    pass
            finally:
                connections.close_all()

        class TransitionAdapter(PreflightAdapter):
            def _handle(self, method, url, **kwargs):
                result = super()._handle(method, url, **kwargs)
                if not started.is_set():
                    transitions.append(pool.submit(transition))
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        with connection.cursor() as cursor:
                            if deployment._exclusive_transition_pending(cursor):
                                return result
                        time.sleep(0.01)
                    raise AssertionError("The deployment transition did not wait for the HTTP mutation")
                return result

        adapter = TransitionAdapter()
        config, session = adapter.patches()
        with ThreadPoolExecutor(max_workers=1) as pool, config, session:
            response = self.client.post(self.preview_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            transitions[0].result(timeout=10)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("precondition was unmet", response.json()["message"])
        self.assertEqual(adapter.preflights, [])
        self.assert_nothing_sent()
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")
        self.assertIsNone(self.row.apply_attempt_id)

    def _overlapping_apply_responses(self, *, success_first, replay=False, reject_second=False, both_success=False):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from django.db import connections

        from netbox_nso_plugin.apply_settlement import _record_replay_answer, _replay_apply_answer
        from netbox_nso_plugin.models import NSOApplyAttempt

        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        counter_lock = threading.Lock()

        class OverlappingAdapter(PreflightAdapter):
            def _handle(adapter, method, url, **kwargs):
                if method != "POST" or not url.endswith("/actions/apply"):
                    return super()._handle(method, url, **kwargs)
                with counter_lock:
                    index = len(adapter.applies)
                    adapter.applies.append(kwargs["json"])
                entered[index].set()
                if not release[index].wait(15):
                    raise AssertionError("The test did not release the Apply response")
                if not both_success and (index == 1) == reject_second:
                    return make_response(401, {"error": {"code": "unauthorized", "message": "Placeholder rejection"}})
                return make_response(202, adapter.apply_response(kwargs["json"]["selected"]))

        adapter = OverlappingAdapter()

        def admitted(selected):
            response = _promoted(selected)
            response["generations"] = response["generations"][:1]
            return response

        adapter.apply_response = admitted
        preview = self.preview(adapter)
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(self.user)

        def send(index):
            try:
                if index == 1 and replay:
                    attempt = NSOApplyAttempt.objects.get(pk=adapter.applies[0]["apply_attempt_id"])
                    answer = _replay_apply_answer(self.mgmt, attempt)
                    if answer is not None:
                        with transaction.atomic():
                            _record_replay_answer(answer[0], result=answer[1], error=answer[2])
                    return None
                return clients[index].post(
                    self.apply_url, {"apply_preview": preview["apply_preview"]}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
                )
            finally:
                connections.close_all()

        config, session = adapter.patches()
        with ThreadPoolExecutor(max_workers=2) as pool, config, session:
            try:
                futures = [pool.submit(send, 0)]
                self.assertTrue(entered[0].wait(10), "The first send did not reach HTTP")
                futures.append(pool.submit(send, 1))
                self.assertTrue(entered[1].wait(10), "The second send did not reach HTTP")
                successful = 0 if reject_second else 1
                first = successful if success_first else 1 - successful
                release[first].set()
                first_response = futures[first].result(timeout=10)
                self.row.refresh_from_db()
                self.assertEqual(self.row.status, "deploying")
                self.assertIsNotNone(self.row.apply_attempt_id)
                attempt = NSOApplyAttempt.objects.get(pk=adapter.applies[0]["apply_attempt_id"])
                if not success_first:
                    self.assertIsNone(attempt.response)
                    if first_response is not None:
                        self.assertEqual(first_response.status_code, 502)
                        self.assertIn("retry", first_response.json()["message"].lower())
                release[1 - first].set()
                futures[1 - first].result(timeout=10)
            finally:
                for event in release:
                    event.set()
        attempt.refresh_from_db()
        self.assertEqual(attempt.http_status, 202)
        self.assertEqual(attempt.send_seq, 2)
        self.assertEqual(attempt.response, adapter.apply_response(attempt.selected))
        self.assertEqual(adapter.applies[0], adapter.applies[1])
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")
        self.assertEqual(self.row.apply_attempt_id, attempt.pk)
        self.assertTrue(attempt.overlay_snapshots.filter(settlement_disposition="unresolved").exists())

    def test_overlapping_confirmation_rejection_before_success_preserves_promotion(self):
        self._overlapping_apply_responses(success_first=False)

    def test_overlapping_confirmation_success_before_rejection_preserves_promotion(self):
        self._overlapping_apply_responses(success_first=True)

    def test_overlapping_reconciliation_rejection_before_success_preserves_promotion(self):
        self._overlapping_apply_responses(success_first=False, replay=True)

    def test_overlapping_reconciliation_success_before_rejection_preserves_promotion(self):
        self._overlapping_apply_responses(success_first=True, replay=True)

    def test_overlapping_later_rejection_cannot_rule_out_the_first_admission(self):
        self._overlapping_apply_responses(success_first=False, reject_second=True)

    def test_overlapping_reconciliation_rejection_cannot_rule_out_the_first_admission(self):
        self._overlapping_apply_responses(success_first=False, replay=True, reject_second=True)

    def test_overlapping_successful_replays_record_the_same_answer(self):
        self._overlapping_apply_responses(success_first=True, replay=True, both_success=True)

    def test_definite_rejection_spends_the_preview_after_authentication_recovers(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        class RecoveringAdapter(PreflightAdapter):
            unauthorized = True

            def _handle(self, method, url, **kwargs):
                if method == "POST" and url.endswith("/actions/apply") and self.unauthorized:
                    self.applies.append(kwargs["json"])
                    return make_response(401, {"error": {"code": "unauthorized", "message": "Placeholder rejection"}})
                return super()._handle(method, url, **kwargs)

        adapter = RecoveringAdapter()
        preview = self.preview(adapter)
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 502, response.content)
        attempt = NSOApplyAttempt.objects.get(pk=adapter.applies[0]["apply_attempt_id"])
        self.assertEqual(attempt.http_status, 401)
        self.assertEqual(
            set(attempt.overlay_snapshots.values_list("settlement_disposition", flat=True)), {"not_applied"}
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")

        adapter.unauthorized = False
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["status"], "preflight_stale")
        self.assertEqual(len(adapter.applies), 1)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "accepted")
        fresh = self.preview(adapter)
        self.assertEqual(self.confirm(adapter, fresh["apply_preview"]).status_code, 200)
        self.assertEqual([body["preflight_identity"] for body in adapter.applies], adapter.identities)
        self.assertNotEqual(adapter.applies[1]["apply_attempt_id"], adapter.applies[0]["apply_attempt_id"])
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")

    def test_ambiguous_submission_and_successful_admission_keep_the_same_preview_replayable(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        class RecoveringAdapter(PreflightAdapter):
            unavailable = True

            def _handle(self, method, url, **kwargs):
                if method == "POST" and url.endswith("/actions/apply") and self.unavailable:
                    self.applies.append(kwargs["json"])
                    return make_response(503, {"error": {"code": "nso_timeout", "message": "Placeholder timeout"}})
                return super()._handle(method, url, **kwargs)

        adapter = RecoveringAdapter()
        preview = self.preview(adapter)
        self.assertEqual(self.confirm(adapter, preview["apply_preview"]).status_code, 502)
        attempt = NSOApplyAttempt.objects.get(pk=adapter.applies[0]["apply_attempt_id"])
        self.assertIsNone(attempt.response)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")
        adapter.unavailable = False
        self.assertEqual(self.confirm(adapter, preview["apply_preview"]).status_code, 200)
        self.assertEqual(self.confirm(adapter, preview["apply_preview"]).status_code, 200)
        self.assertEqual(adapter.applies, [adapter.applies[0]] * 3)
        self.assertEqual(len(adapter.preflights), 1)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")

    def test_lost_admission_then_unauthorized_replay_preserves_snapshots_until_settlement(self):
        from netbox_nso_plugin.apply_settlement import settle_apply_attempts
        from netbox_nso_plugin.models import NSOApplyAttempt

        from ._bgp_settlement import promoted_streams
        from .test_apply_settlement import _generation, _payload

        adapter = ReplayAdmissionAdapter(lose_response=True)
        adapter.apply_response = lambda selected: promoted_streams(selected, {"vlan"})
        preview = self.preview(adapter)
        self.assertEqual(self.confirm(adapter, preview["apply_preview"]).status_code, 502)
        attempt = NSOApplyAttempt.objects.get(pk=adapter.applies[0]["apply_attempt_id"])
        prior_snapshots = list(attempt.overlay_snapshots.values())
        adapter.unauthorized = True
        replay = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(replay.status_code, 502, replay.content)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.response)
        self.assertIsNone(attempt.http_status)
        self.assertEqual(list(attempt.overlay_snapshots.values()), prior_snapshots)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")
        self.assertIn("retry", replay.json()["message"].lower())

        snapshot = attempt.overlay_snapshots.get(overlay_pk=self.row.pk, model_label=self.row._meta.label_lower)
        ref = snapshot.contributors["rows"][0]["logical_ref"]
        carrier = {
            "stream": "vlan",
            "row": {"subject": ref["subject"], "key": ref["key"]},
            "source_push_seq": attempt.selected["vlan"],
            "frozen_revision": 7,
            "accepted_at": None,
            "generation_id": 81,
            "settlement_cohort": 9,
        }
        outcome = {key: value for key, value in carrier.items() if key != "accepted_at"}
        outcome.update(acceptance_identity=None, carrier=carrier, disposition="in_sync", refusals=[])
        result = {
            "row_outcomes": [outcome],
            "refusals": [],
            "commit_acknowledged": True,
            "settlement_owners": [{**carrier, "disposition": "in_sync"}],
            "vlan_count_by_outcome": {"in_sync": 1, "apply_failed": 0, "not_applied": 0, "unavailable": 0},
        }
        generation = _generation(81, "settled", {"vlan": attempt.selected["vlan"]}, result=result)
        generation.update(seq=4, stream_revisions={"vlan": 7}, carrier_job_id=501)
        evidence = _payload(
            self.mgmt.adapter_device_id,
            [
                {
                    "apply_attempt_id": str(attempt.pk),
                    "admission_state": "admitted",
                    "http_status": 202,
                    "response": adapter.admission,
                    "generations": [generation],
                }
            ],
        )
        evidence.settlement_sequences = {501: 1}
        settle_apply_attempts(self.mgmt, evidence)
        attempt.refresh_from_db()
        self.assertEqual(attempt.response, adapter.admission)
        self.assertEqual(attempt.http_status, 202)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "in_sync")

    def test_recorded_admission_then_unauthorized_replay_leaves_state_unchanged(self):
        from netbox_nso_plugin.models import NSOApplyAttempt

        adapter = ReplayAdmissionAdapter()
        preview = self.preview(adapter)
        self.assertEqual(self.confirm(adapter, preview["apply_preview"]).status_code, 200)
        attempt = NSOApplyAttempt.objects.get(pk=adapter.applies[0]["apply_attempt_id"])
        before_attempt = NSOApplyAttempt.objects.values().get(pk=attempt.pk)
        before_snapshots = list(attempt.overlay_snapshots.values())
        self.row.refresh_from_db()
        before_row = (self.row.status, self.row.apply_attempt_id, self.row.last_apply_at, self.row.last_apply_error)
        adapter.unauthorized = True
        response = self.confirm(adapter, preview["apply_preview"])
        self.assertEqual(response.status_code, 502, response.content)
        self.assertIn("retry", response.json()["message"].lower())
        before_attempt["send_seq"] += 1
        self.assertEqual(NSOApplyAttempt.objects.values().get(pk=attempt.pk), before_attempt)
        self.assertEqual(list(attempt.overlay_snapshots.values()), before_snapshots)
        self.row.refresh_from_db()
        self.assertEqual(
            (self.row.status, self.row.apply_attempt_id, self.row.last_apply_at, self.row.last_apply_error), before_row
        )

    def test_nonready_preview_admits_nothing_and_cannot_confirm(self):
        from django.core import signing

        from netbox_nso_plugin.models import NSOApplyAttempt
        from netbox_nso_plugin.views import _APPLY_PREVIEW_SALT

        cases = (
            ("unavailable", None, "Apply preflight is unavailable. No stream was admitted. Try a fresh preview."),
            (
                "error",
                None,
                "Apply preflight failed. No stream was admitted. Review the adapter state and try a fresh preview.",
            ),
            ("refused", "baseline_refused", "Apply refused: the authorized baseline needs repair or Release: ip"),
            ("refused", "cross_stream", "Apply refused: the selection causes a refusal in another stream: ip"),
        )
        for status, diagnosis, message in cases:
            with self.subTest(status=status, diagnosis=diagnosis):

                def verdict(selected):
                    result = preflight_wire(selected, status=status)
                    if diagnosis:
                        result[diagnosis] = ["ip"]
                    return result

                adapter = PreflightAdapter(verdict)
                earlier = set(NSOApplyAttempt.objects.filter(management=self.mgmt).values_list("pk", flat=True))
                result = self.preview(adapter)
                self.assertFalse(result["can_apply"])
                self.assertIsNone(result["apply_preview"])
                self.assertEqual(result["message"], message)
                attempt = (
                    NSOApplyAttempt.objects.filter(management=self.mgmt, trigger_kind="manual")
                    .exclude(pk__in=earlier)
                    .get()
                )
                self.assertEqual(attempt.selected, {})
                self.assertEqual(attempt.preflight["result"]["status"], status)
                # The view issues no token for this attempt, so the test signs one to prove the confirm guard.
                token = signing.dumps(
                    {"attempt": str(attempt.pk), "user": self.user.pk, "management": self.mgmt.pk},
                    salt=_APPLY_PREVIEW_SALT,
                )
                response = self.confirm(adapter, token)
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json()["status"], "preflight_stale")
                self.assertEqual(adapter.applies, [])
                self.assert_nothing_sent()
                self.row.refresh_from_db()
                self.assertEqual(self.row.status, "accepted")

    def test_expired_capture_after_preflight_failure_is_pruned(self):
        from datetime import timedelta

        import requests
        from django.utils import timezone

        from netbox_nso_plugin.apply_retention import prune_apply_ledger
        from netbox_nso_plugin.models import NSOApplyAttempt, NSOApplyOverlaySnapshot

        class FailingPreflightAdapter(PreflightAdapter):
            failure = "timeout"

            def _handle(self, method, url, **kwargs):
                if method == "POST" and url.endswith("/actions/apply-preflight"):
                    if self.failure == "timeout":
                        raise requests.exceptions.Timeout("Placeholder timeout")
                    if self.failure == "conflict":
                        return make_response(409, {"error": {"code": "conflict", "message": "Placeholder conflict"}})
                    return make_response(200, {})
                return super()._handle(method, url, **kwargs)

        remote = FakeAdapter()
        self.addCleanup(remote.stop)
        remote.store.add_device(nso_instance="placeholder", nso_device_name="placeholder", device_id=1558)
        adapter = FailingPreflightAdapter()
        for failure, expected_status in (("timeout", 502), ("conflict", 409), ("malformed", 502)):
            with self.subTest(failure=failure):
                adapter.failure = failure
                adapter.preparation_revisions = self.preparation_revisions
                config, session = adapter.patches()
                with config, session:
                    response = self.client.post(self.preview_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(response.status_code, expected_status, response.content)
                attempt = NSOApplyAttempt.objects.filter(management=self.mgmt).latest("created_at")
                self.assertIsNone(attempt.preflight)
                self.assertIsNone(attempt.adapter_apply_attempt_id)
                self.assertEqual(attempt.lifecycle, "open")
                self.assertTrue(attempt.overlay_snapshots.exists())
                self.row.refresh_from_db()
                self.assertEqual(self.row.status, "accepted")
                self.assertIsNone(self.row.apply_attempt_id)
                NSOApplyAttempt.objects.filter(pk=attempt.pk).update(created_at=timezone.now() - timedelta(days=31))
                with (
                    patch("netbox_nso_plugin.adapter_client.requests.Session", LoopbackOnlySession),
                    patch("netbox_nso_plugin.adapter_client._resolve_config", return_value={**CFG, "url": remote.url}),
                ):
                    result = prune_apply_ledger(execute=True)
                self.assertEqual(result.deleted, 1)
                self.assertFalse(NSOApplyAttempt.objects.filter(pk=attempt.pk).exists())
                self.assertFalse(NSOApplyOverlaySnapshot.objects.filter(attempt_id=attempt.pk).exists())

    def test_tampered_preview_and_another_operators_preview_cannot_apply(self):
        adapter = PreflightAdapter()
        result = self.preview(adapter)
        response = self.confirm(adapter, result["apply_preview"] + "tampered")
        self.assertEqual(response.status_code, 409)
        other = get_user_model().objects.create_superuser("other-preflight-operator", password="placeholder-password")
        self.client.force_login(other)
        self.assertEqual(self.confirm(adapter, result["apply_preview"]).status_code, 409)
        self.assertEqual(adapter.applies, [])

    def test_blocked_interface_stays_accepted_with_catalog_reason_while_vlan_deploys(self):
        import json
        from pathlib import Path

        from dcim.models import Interface

        from netbox_nso_plugin.models import NSOInterfaceState
        from netbox_nso_plugin.refusal_display import row_projection

        from ._ownership_case import acquire_overlay

        with without_commit_drain(), transaction.atomic():
            self.mgmt.manage_interfaces = True
            self.mgmt.manage_description = True
            self.mgmt.save(update_fields=("manage_interfaces", "manage_description"))
            iface = Interface.objects.create(
                device=self.device, name="Ethernet1", type="1000base-t", description="placeholder desired"
            )
            blocked = acquire_overlay(
                NSOInterfaceState,
                interface=iface,
                attribute="description",
                status="accepted",
                nso_value="placeholder observed",
            )
        corpus = json.loads((Path(__file__).parents[1] / "refusal_protocol_corpus.json").read_text(encoding="utf-8"))
        cause = next(
            record for case in corpus["valid"] for record in case["refusals"] if record["family"] == "interface"
        )
        refusal = {**cause, "logical_rows": [], "provenance": "current", "sender_phase": "pre_commit_dry_run"}
        adapter = PreflightAdapter(
            lambda selected: preflight_wire(
                selected,
                blocked={
                    "interface_config": {"status": "blocked", "refusals": [refusal]},
                },
            )
        )
        preview = self.preview(adapter)
        self.assertIn("inexpressible-on-ned", preview["streams"]["interface_config"]["message"])
        blocked.refresh_from_db()
        self.assertEqual(blocked.status, "accepted")
        self.assertIsNone(blocked.apply_attempt_id)
        self.assertIn("inexpressible-on-ned", row_projection(blocked)["refusal_text"])
        self.assertEqual(self.confirm(adapter, preview["apply_preview"]).status_code, 200)
        self.assertNotIn("interface_config", adapter.applies[0]["selected"])
        blocked.refresh_from_db()
        self.assertEqual(blocked.status, "accepted")
        self.assertIsNone(blocked.apply_attempt_id)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "deploying")

    def test_ip_refusal_keeps_the_acquired_ip_pending_and_admits_interface_attributes(self):
        from dcim.models import Interface
        from django.utils import timezone
        from ipam.models import IPAddress

        from netbox_nso_plugin.models import NSOInterfaceIPState, NSOInterfaceState, NSOOwnershipManifest
        from netbox_nso_plugin.refusal_display import row_projection
        from netbox_nso_plugin.refusal_protocol import MESSAGES, row_ref

        from ._ownership_case import acquire_overlay

        with without_commit_drain(), transaction.atomic():
            self.mgmt.manage_interfaces = True
            self.mgmt.manage_description = True
            self.mgmt.save(update_fields=("manage_interfaces", "manage_description"))
            interface = Interface.objects.create(
                device=self.device, name="me0", type="1000base-t", description="placeholder desired"
            )
            IPAddress.objects.create(address="198.18.0.1/24", assigned_object=interface)
            address = acquire_overlay(
                NSOInterfaceIPState,
                interface=interface,
                address="198.18.0.1/24",
                status="accepted",
                accepted_at=timezone.now(),
            )
            description = acquire_overlay(
                NSOInterfaceState,
                interface=interface,
                attribute="description",
                status="accepted",
                nso_value="placeholder observed",
            )
        key = row_ref(
            "interface", "interface.ipv4-address", (("interface", ("me0",)), ("ipv4-address", ("198.18.0.1",)))
        )
        catalog = MESSAGES["write-blocked"]

        def verdict(selected):
            identity = {
                "stream": "ip",
                "row": {"subject": key.subject, "key": key.key},
                "source_push_seq": selected["ip"],
                "frozen_revision": 1,
                "generation_id": None,
                "settlement_cohort": None,
            }
            logical = {
                **identity,
                "acceptance_identity": address.accepted_at.isoformat(),
                "carrier": {**identity, "accepted_at": address.accepted_at.isoformat()},
            }
            refusal = {
                "family": "interface",
                "row": identity["row"],
                "field": "interface.ip.v4",
                "reason": catalog.reason,
                "message_id": catalog.id,
                "message": catalog.text,
                "related": None,
                "logical_rows": [logical],
                "provenance": "current",
                "sender_phase": "pre_commit_dry_run",
            }
            return preflight_wire(selected, blocked={"ip": {"status": "blocked", "refusals": [refusal]}})

        adapter = PreflightAdapter(verdict)
        preview = self.preview(adapter)
        self.assertEqual(preview["streams"]["interface_config"]["status"], "admitted")
        self.assertTrue(NSOOwnershipManifest.objects.filter(device_id=self.device.pk).exists())
        request = next(request for request in adapter.requests if request["url"].endswith("/intent"))
        self.assertIn("placeholder desired", str(request["body"]))
        ip_request = next(request for request in adapter.requests if request["url"].endswith("/ip-intent"))
        self.assertIn("198.18.0.1/24", str(ip_request["body"]))
        self.assertEqual(self.confirm(adapter, preview["apply_preview"]).status_code, 200)
        self.assertNotIn("ip", adapter.applies[0]["selected"])
        self.assertIn("interface_config", adapter.applies[0]["selected"])
        address.refresh_from_db()
        description.refresh_from_db()
        self.assertEqual(address.status, "accepted")
        self.assertIsNone(address.apply_attempt_id)
        self.assertIn(catalog.reason, row_projection(address)["error"])
        self.assertEqual(description.status, "deploying")

    def test_terminal_diagnostics_explain_the_admission_stop_without_device_text(self):
        cases = (
            ("error", "attribution_failed", "cannot be attributed"),
            ("error", "body_build_failed", "could not build"),
            ("refused", "removal_blocked_collateral", "outside this selection"),
            ("unavailable", "inputs_changed_during_preflight", "inputs changed"),
        )
        for status, code, wording in cases:
            with self.subTest(code=code):

                def verdict(selected):
                    result = preflight_wire(selected, status=status)
                    result["error"] = {"code": code, "message": "private device diagnostic canary"}
                    return result

                adapter = PreflightAdapter(verdict)
                preview = self.preview(adapter)
                self.assertFalse(preview["can_apply"])
                self.assertIn(wording, preview["message"])
                self.assertNotIn("private device diagnostic canary", str(preview))
                self.assert_nothing_sent()
