# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Current generation previews through real NetBox HTTP and the adapter transport."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from netbox_nso_plugin import adapter_client

from . import test_removal_blocked_surfacing as blocked
from ._adapter_http import make_apply_preview as _preview
from ._adapter_http import make_response, make_transport_session


class TestApplyPreviewHTTP(blocked.BlockedRemovalTestBase):
    """External HTTP is the only substituted boundary."""

    def _get_preview(self, payload, *, outformat="native", error=None):
        from django.urls import reverse

        session = make_transport_session(make_response(json_data=payload), error=error)
        self.addCleanup(session.close)
        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=blocked._ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
        ):
            response = self.client.get(
                reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk]),
                {"outformat": outformat},
            )
        session.send.assert_called_once()
        prepared = session.send.call_args.args[0]
        self.assertEqual(prepared.method, "GET")
        parsed = urlsplit(prepared.url)
        self.assertEqual(parsed.path, "/api/v1/devices/10/actions/apply-diff")
        self.assertEqual(parse_qs(parsed.query), {"outformat": ["cli" if outformat == "cli" else "native"]})
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        return response.json()

    def test_current_deployment_preview_keeps_identity_and_text(self):
        delta = "<edit-config>\ninterface example0\n</edit-config>"
        result = self._get_preview(_preview(diffs={"device_intent": delta}))

        self.assertEqual(result["device_diff"], {"device_intent": delta})
        self.assertEqual(result["generation_id"], 81)
        self.assertEqual(result["document_digest"], "a" * 64)
        self.assertTrue(result["diff_available"])
        self.assertIsNone(result["diff_error"])
        self.assertFalse(result["nothing_pending"])

    def test_empty_generation_is_a_successful_preview(self):
        result = self._get_preview(_preview())

        self.assertEqual(result["device_diff"], {})
        self.assertTrue(result["diff_available"])
        self.assertTrue(result["nothing_pending"])
        self.assertEqual(result["generation_id"], 81)
        self.assertIsNone(result["diff_error"])

    def test_cli_format_uses_the_same_generation_contract(self):
        result = self._get_preview(_preview(outformat="cli"), outformat="cli")

        self.assertEqual(result["outformat"], "cli")
        self.assertTrue(result["diff_available"])

    def test_unknown_requested_format_uses_native(self):
        result = self._get_preview(_preview(), outformat="invalid")

        self.assertEqual(result["outformat"], "native")
        self.assertTrue(result["diff_available"])

    def test_http_success_unavailable_marker_is_not_an_empty_success(self):
        marker = "!! preview unavailable: executable generation changed during preview"
        for identity in ((None, None), (81, "a" * 64)):
            with self.subTest(identity=identity):
                result = self._get_preview(
                    _preview(diffs={"device_intent": marker}, generation_id=identity[0], document_digest=identity[1])
                )

                self.assertFalse(result["diff_available"])
                self.assertFalse(result["nothing_pending"])
                self.assertEqual(result["device_diff"], {})
                self.assertEqual(result["diff_error"], "preview_unavailable")
                self.assertEqual(result["generation_id"], identity[0])
                self.assertEqual(result["document_digest"], identity[1])

    def test_malformed_previews_fail_at_the_client_boundary(self):
        malformed = [
            None,
            [],
            {},
            {**_preview(), "device_id": 11},
            {**_preview(), "device_id": True},
            {**_preview(), "outformat": "cli"},
            {**_preview(), "diffs": []},
            {**_preview(), "diffs": {"isis": "interface example0"}},
            {**_preview(), "diffs": {"device_intent": 3}},
            {**_preview(), "diffs": {"device_intent": "delta", "vlan": "delta"}},
            {**_preview(), "generation_id": True},
            {**_preview(), "generation_id": 0},
            {**_preview(), "generation_id": -1},
            {**_preview(), "generation_id": None},
            {**_preview(), "document_digest": None},
            {**_preview(), "document_digest": "g" * 64},
            {**_preview(), "document_digest": "A" * 64},
            {**_preview(), "document_digest": "a" * 63},
            {**_preview(), "document_digest": 3},
            {**_preview(), "generation_id": None, "document_digest": None},
        ]
        marker = {"device_intent": "!! preview unavailable: no generation"}
        malformed.append(_preview(diffs=marker, generation_id=None))
        malformed.extend({key: value for key, value in _preview().items() if key != field} for field in _preview())
        for payload in malformed:
            with self.subTest(payload=payload):
                session = make_transport_session(make_response(json_data=payload))
                self.addCleanup(session.close)
                with (
                    patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=blocked._ADAPTER_CFG),
                    patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
                    self.assertRaises(adapter_client.AdapterError) as raised,
                ):
                    adapter_client.get_apply_diff(10)
                self.assertEqual(raised.exception.code, "invalid_response")

    def test_malformed_preview_reports_a_distinct_failure(self):
        result = self._get_preview(_preview(generation_id=None, document_digest=None))

        self.assertEqual(result["diff_error"], "invalid_response")
        self.assertFalse(result["diff_available"])
        self.assertFalse(result["nothing_pending"])
        self.assertEqual(result["device_diff"], {})
        self.assertIsNone(result["generation_id"])

    def test_transport_failure_reports_a_distinct_failure(self):
        result = self._get_preview(None, error=requests.ConnectionError("external boundary unavailable"))

        self.assertEqual(result["diff_error"], "nso_unreachable")
        self.assertFalse(result["diff_available"])
        self.assertFalse(result["nothing_pending"])
        self.assertEqual(result["device_diff"], {})

    def test_error_body_and_exception_text_do_not_enter_logs_or_json(self):
        sentinel = "native-delta-private-sentinel"
        session = make_transport_session(make_response(status_code=503, json_data={"error": {"message": sentinel}}))
        self.addCleanup(session.close)
        from django.urls import reverse

        with (
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=blocked._ADAPTER_CFG),
            patch("netbox_nso_plugin.adapter_client.requests.Session", return_value=session),
            self.assertLogs("netbox_nso_plugin.views", level="DEBUG") as captured,
        ):
            response = self.client.get(reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk]))

        self.assertNotIn(sentinel, response.content.decode())
        self.assertNotIn(sentinel, "\n".join(captured.output))

    def test_missing_management_does_not_call_adapter_and_is_unavailable(self):
        from django.urls import reverse

        self.mgmt.delete()
        with patch("netbox_nso_plugin.adapter_client.requests.Session") as session_class:
            response = self.client.get(reverse("plugins:netbox_nso_plugin:device_apply_preview", args=[self.device.pk]))
        session_class.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertFalse(response.json()["diff_available"])
        self.assertFalse(response.json()["nothing_pending"])
        self.assertEqual(response.json()["diff_error"], "preview_unavailable")
