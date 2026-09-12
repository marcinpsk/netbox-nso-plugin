# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A caught exception's text never reaches an HTTP response.

Every adapter failure is rendered by some view, so ``adapter_client`` converts the
transport exception into an ``AdapterError`` that carries the exception TYPE only, and the
blanket ``except`` guards in ``onboarding`` follow the same rule. The transport text is
dropped rather than logged: it echoes the failed request, headers included. These tests
drive the real views through the real ``adapter_client`` (only the network
``requests.Session`` is replaced) and assert the leaked text is gone from the body.

Also covers the sibling reflection bug: an unknown category key is a raw URL segment echoed
into an HTML body, so it must come back escaped.
"""

import json
from unittest.mock import patch

import requests
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Platform, Site
from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress

from netbox_nso_plugin.adapter_client import AdapterError
from netbox_nso_plugin.models import NSOInstance, NSOPlatformNedMapping

from ._adapter_http import make_response
from ._outbox_case import mirror_update
from .test_django_views import ViewTestBase

# Shaped like something that must never be echoed to a client: a requests transport error
# repeats the request it failed on, and an unexpected internal error can repeat a row value.
_LEAK = "Bearer nso-adapter-token-abc123 must never leave the server"
# A token that makes requests' own header validation echo it back (InvalidHeader repeats the
# value). The secret part is one unbroken word so a repr()'d copy still matches.
_HEADER_TOKEN_SECRET = "nso-adapter-token-must-never-be-logged"
_HEADER_BREAKING_TOKEN = f"{_HEADER_TOKEN_SECRET}\nX-Leak: yes"
_ADAPTER_LOG = "netbox_nso_plugin.adapter_client"
_ONBOARDING_LOG = "netbox_nso_plugin.onboarding"
_VIEWS_LOG = "netbox_nso_plugin.views"
_AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
_PUBLIC_ADAPTER_ERROR = "The NSO adapter request failed. See the server log."
_PUBLIC_INVALID_RESPONSE = "The NSO adapter returned an invalid response. See the server log."
_PLUGINS_CONFIG = {
    **settings.PLUGINS_CONFIG,
    "netbox_nso_plugin": {"adapter_url": "http://adapter.invalid", "adapter_token": "envelope-test-token"},
}


class _LeakingSession(requests.Session):
    """A real Session whose every HTTP call fails with a transport error carrying ``_LEAK``.

    A real subclass, not a MagicMock: ``get``/``post``/``put`` all delegate to ``request``,
    so overriding it blocks the whole surface instead of one stubbed method.
    """

    def request(self, *args, **kwargs):
        raise requests.exceptions.ConnectionError(_LEAK)


class _UnreachableAdapterMixin:
    """Point adapter_client at the leaking transport for the duration of one test."""

    session_class = _LeakingSession

    def setUp(self):
        super().setUp()
        import netbox_nso_plugin.adapter_client as ac

        ac.reset_config_cache()
        ac.reset_session()
        self.addCleanup(ac.reset_session)
        self.addCleanup(ac.reset_config_cache)
        settings_patch = override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        session_patch = patch("netbox_nso_plugin.adapter_client.requests.Session", self.session_class)
        session_patch.start()
        self.addCleanup(session_patch.stop)

    def assertEnvelopeOnly(self, body, *, expected_type="ConnectionError"):
        """The body names the exception type but carries none of its text."""
        self.assertNotIn(_LEAK, body)
        self.assertIn(expected_type, body)


class TestAdapterTransportEnvelope(_UnreachableAdapterMixin, TestCase):
    """adapter_client is where the transport exception is converted, so it is tested there."""

    def test_transport_error_text_never_enters_the_adapter_error(self):
        """The AdapterError names the transport exception type; the text is dropped."""
        from netbox_nso_plugin.adapter_client import _request

        with self.assertLogs(_ADAPTER_LOG, level="WARNING") as logs:
            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")

        self.assertEqual(ctx.exception.code, "nso_unreachable")
        self.assertEnvelopeOnly(str(ctx.exception))
        self.assertTrue(
            any("ConnectionError" in line for line in logs.output), "the log must name the transport exception type"
        )
        self.assertFalse(any(_LEAK in line for line in logs.output), "the transport text must not reach the log")


class TestAdapterTransportLogRedaction(TestCase):
    """The transport text never reaches the SERVER LOG either: it can carry the bearer token.

    ``requests`` validates header values while preparing the request, and ``InvalidHeader``
    repeats the offending value. A configured token holding a newline therefore raises before
    any socket is opened, with the whole ``Authorization`` value inside the exception message.
    """

    def test_invalid_header_never_writes_the_token_to_the_log(self):
        import netbox_nso_plugin.adapter_client as ac

        cfg = {
            "url": "http://adapter.invalid",
            "token": _HEADER_BREAKING_TOKEN,
            "verify_tls": True,
            "ca_cert_path": None,
            "timeout": 5,
        }
        ac.reset_session()
        self.addCleanup(ac.reset_session)

        # The real Session, so requests' own header validation raises: under pytest the
        # hermetic-network fixture has replaced ``requests.Session``, and
        # ``requests.sessions.Session`` is the untouched class behind it.
        with (
            patch("netbox_nso_plugin.adapter_client.requests.Session", requests.sessions.Session),
            patch("netbox_nso_plugin.adapter_client._resolve_config", return_value=cfg),
            self.assertLogs(_ADAPTER_LOG, level="WARNING") as logs,
            self.assertRaises(AdapterError) as ctx,
        ):
            ac._request("GET", "/test")

        self.assertEqual(ctx.exception.code, "nso_unreachable")
        self.assertIn("InvalidHeader", str(ctx.exception))
        for line in logs.output:
            self.assertNotIn(_HEADER_TOKEN_SECRET, line, "the bearer token reached the server log")
            self.assertNotIn("X-Leak", line, "the injected header reached the server log")
        self.assertTrue(any("InvalidHeader" in line for line in logs.output), logs.output)


class TestAdapterErrorEnvelopeInResponses(_UnreachableAdapterMixin, ViewTestBase):
    """Every view maps adapter failures to a fixed public message."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A suppressed mirror write, so the post_save adapter push does not fire during fixture setup.
        mirror_update(cls.mgmt, adapter_device_id=4242)
        cls.mgmt.refresh_from_db()

    def _url(self, name, **kwargs):
        return reverse(f"plugins:netbox_nso_plugin:{name}", kwargs=kwargs)

    def _response_cases(self):
        """(label, url, method, data) for every view that renders an adapter failure."""
        category = self._url("device_nso_category", pk=self.device.pk, key="bgp")
        interface = self._url("device_nso_category", pk=self.device.pk, key="interface")
        return [
            ("NSODeviceNamesView", self._url("ajax_nso_device_names", instance_pk=self.nso_instance.pk), "get", None),
            ("NSOJobStatusView", self._url("nsojob_status", job_id=7), "get", None),
            ("NSODeviceJobsView", self._url("device_nso_jobs", pk=self.device.pk), "get", None),
            ("NSOCategoryView grid", f"{category}?refresh=1", "get", None),
            ("NSOCategoryView interface JSON", f"{interface}?refresh=1&format=json", "get", None),
            (
                "NSODeviceActionView",
                self._url("nsodevicemanagement_action", pk=self.mgmt.pk, action="sync"),
                "post",
                {},
            ),
            (
                "NSOForceRemovalView",
                self._url("nsodevicemanagement_force_removal", pk=self.mgmt.pk),
                "post",
                {"scope": "static_route"},
            ),
        ]

    def test_no_response_carries_exception_derived_text(self):
        """Each site serves an allowlisted message, while the server log keeps diagnostics."""
        for label, url, method, data in self._response_cases():
            with self.subTest(site=label):
                with self.assertLogs(_ADAPTER_LOG, level="WARNING") as logs:
                    if method == "get":
                        resp = self.client.get(url, **_AJAX)
                    else:
                        resp = self.client.post(url, data, **_AJAX)
                body = resp.content.decode()
                self.assertNotIn(_LEAK, body)
                self.assertNotIn("ConnectionError", body)
                self.assertIn(_PUBLIC_ADAPTER_ERROR, body)
                self.assertFalse(any(_LEAK in line for line in logs.output))

    def test_onboard_status_poll_reports_a_fixed_public_error(self):
        """A transient adapter outage while polling keeps the row provisioning, with no leak."""
        mirror_update(self.mgmt, onboard_status="provisioning", onboard_job_id="job-42")

        with self.assertLogs(_ADAPTER_LOG, level="WARNING"):
            resp = self.client.post(self._url("onboard_status", pk=self.mgmt.pk), **_AJAX)

        body = resp.json()
        self.assertEqual(body["status"], "provisioning")
        self.assertEqual(body["poll_error"], _PUBLIC_ADAPTER_ERROR)

    def test_onboarding_api_reports_a_fixed_public_error(self):
        """The API must not copy the dashboard's caught exception into its response."""
        url = reverse("plugins-api:netbox_nso_plugin-api:onboarding_candidates")

        with self.assertLogs(_ADAPTER_LOG, level="WARNING"):
            resp = self.client.get(url, {"instance": self.nso_instance.adapter_instance_id})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["error"], _PUBLIC_ADAPTER_ERROR)


@override_settings(PLUGINS_CONFIG=_PLUGINS_CONFIG)
class TestOnboardingErrorEnvelope(TestCase):
    """The blanket ``except`` guards in onboarding report a type, never the exception text."""

    @classmethod
    def setUpTestData(cls):
        mfg = Manufacturer.objects.create(name="EnvMfg", slug="envmfg")
        dt = DeviceType.objects.create(manufacturer=mfg, model="EnvDev", slug="envdev")
        role = DeviceRole.objects.create(name="EnvRole", slug="envrole")
        site = Site.objects.create(name="EnvSite", slug="envsite")
        platform = Platform.objects.create(name="EnvPlat", slug="envplat")
        NSOPlatformNedMapping.objects.create(platform=platform, ned_id="cisco-ios-cli-6.114")
        cls.instance = NSOInstance.objects.create(name="env-nso", adapter_instance_id="env-nso-id")
        cls.device = Device.objects.create(
            name="env-router-01", device_type=dt, role=role, site=site, status="active", platform=platform
        )
        iface = Interface.objects.create(device=cls.device, name="mgmt0", type="virtual")
        cls.device.primary_ip4 = IPAddress.objects.create(address="192.0.2.10/24", assigned_object=iface)
        cls.device.save()

    def assertEnvelopeOnly(self, text, *, expected_type):
        self.assertNotIn(_LEAK, text)
        self.assertIn(expected_type, text)

    def test_dashboard_hides_an_unexpected_listing_failure(self):
        """build_onboarding_dashboard's defensive guard reports the type, not repr(exc)."""
        from netbox_nso_plugin.onboarding import build_onboarding_dashboard

        with (
            patch("netbox_nso_plugin.adapter_client.list_instance_devices", side_effect=RuntimeError(_LEAK)),
            self.assertLogs(_ONBOARDING_LOG, level="ERROR") as logs,
        ):
            data = build_onboarding_dashboard(self.instance)

        self.assertEnvelopeOnly(data["error"], expected_type="RuntimeError")
        self.assertTrue(any(_LEAK in line for line in logs.output))

    def test_onboard_hides_an_unexpected_provision_failure(self):
        """onboard_candidate's provision guard reports the type, not repr(exc)."""
        from netbox_nso_plugin.onboarding import onboard_candidate

        with (
            patch("netbox_nso_plugin.adapter_client.provision_device", side_effect=RuntimeError(_LEAK)),
            self.assertLogs(_ONBOARDING_LOG, level="ERROR") as logs,
        ):
            result = onboard_candidate(self.device, self.instance)

        self.assertFalse(result["ok"])
        self.assertEnvelopeOnly(result["error"], expected_type="RuntimeError")
        self.assertTrue(any(_LEAK in line for line in logs.output))

    def test_onboard_hides_an_unexpected_tracking_row_failure(self):
        """The job id stays operator-visible; the DB exception text does not."""
        from netbox_nso_plugin.onboarding import onboard_candidate

        with (
            patch("netbox_nso_plugin.adapter_client.provision_device", return_value={"job_id": "job-77"}),
            patch("netbox_nso_plugin.management_lifecycle.save_management", side_effect=RuntimeError(_LEAK)),
            self.assertLogs(_ONBOARDING_LOG, level="ERROR") as logs,
        ):
            result = onboard_candidate(self.device, self.instance)

        self.assertFalse(result["ok"])
        self.assertEqual(result["job_id"], "job-77")
        self.assertIn("job-77", result["error"])
        self.assertEnvelopeOnly(result["error"], expected_type="RuntimeError")
        self.assertTrue(any(_LEAK in line for line in logs.output))

    def test_manage_existing_hides_an_unexpected_row_failure(self):
        """manage_existing's guard reports the type, not repr(exc)."""
        from netbox_nso_plugin.onboarding import manage_existing

        with (
            patch("netbox_nso_plugin.management_lifecycle.save_management", side_effect=RuntimeError(_LEAK)),
            self.assertLogs(_ONBOARDING_LOG, level="ERROR") as logs,
        ):
            result = manage_existing(self.device, self.instance, "env-router-01")

        self.assertFalse(result["ok"])
        self.assertEnvelopeOnly(result["error"], expected_type="RuntimeError")
        self.assertTrue(any(_LEAK in line for line in logs.output))


class TestOnboardActionEnvelope(_UnreachableAdapterMixin, ViewTestBase):
    """The onboard/manage action views never render an escaped exception's text either."""

    def test_onboard_action_reports_the_type_not_the_exception_text(self):
        """A failure escaping onboard_candidate becomes a typed message, logged in full."""
        url = reverse("plugins:netbox_nso_plugin:onboard")
        with (
            patch("netbox_nso_plugin.onboarding.onboard_candidate", side_effect=RuntimeError(_LEAK)),
            self.assertLogs(_VIEWS_LOG, level="ERROR") as logs,
        ):
            resp = self.client.post(
                url, {"device": self.device.pk, "instance": self.nso_instance.adapter_instance_id}, follow=True
            )

        body = resp.content.decode()
        self.assertNotIn(_LEAK, body)
        self.assertIn("RuntimeError", body)
        self.assertTrue(any(_LEAK in line for line in logs.output))

    def test_manage_action_reports_the_type_not_the_exception_text(self):
        """Same for the quick-manage action's guard."""
        url = reverse("plugins:netbox_nso_plugin:quick_manage")
        with (
            patch("netbox_nso_plugin.onboarding.manage_existing", side_effect=RuntimeError(_LEAK)),
            self.assertLogs(_VIEWS_LOG, level="ERROR") as logs,
        ):
            resp = self.client.post(
                url,
                {
                    "device": self.device.pk,
                    "instance": self.nso_instance.adapter_instance_id,
                    "nso_name": "view-router-01",
                },
                follow=True,
            )

        body = resp.content.decode()
        self.assertNotIn(_LEAK, body)
        self.assertIn("RuntimeError", body)
        self.assertTrue(any(_LEAK in line for line in logs.output))


class TestUnknownCategoryKeyIsEscaped(ViewTestBase):
    """The unknown-category 400 echoes a raw URL segment, so it must escape it."""

    _PAYLOAD = "<img src=x onerror=alert(1)>"

    def test_script_payload_in_the_category_key_comes_back_escaped(self):
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": self._PAYLOAD},
        )

        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 400)
        body = resp.content.decode()
        self.assertNotIn(self._PAYLOAD, body)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", body)


class _MalformedJobSession(requests.Session):
    """A real Session answering every call 200 with a body the adapter's JobOut cannot produce.

    A scalar in a ``dict | None`` member is the shape the client refuses at its boundary; the
    point of these tests is what each caller does when it does.
    """

    #: Not a list, and its ``error`` is a scalar — malformed as a single job and as a listing.
    BODY = {"id": 7, "type": "apply", "status": "failed", "result": None, "error": "boom", "context": None}

    def request(self, *args, **kwargs):
        return make_response(200, json_data=self.BODY)


class TestMalformedAdapterPayloadIsRefused(_UnreachableAdapterMixin, ViewTestBase):
    """A payload the contract forbids becomes a typed refusal, never an AttributeError 500.

    The client validates job payloads once (``_validated_job``) rather than every reader
    tolerating a scalar, so these assert the refusal reaches the operator through each
    caller's existing ``except AdapterError`` leg. The views are driven through
    ``RequestFactory`` rather than the test client: these endpoints answer ``JsonResponse``
    with no template, so routing adds nothing the assertions depend on.
    """

    session_class = _MalformedJobSession

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        mirror_update(cls.mgmt, adapter_device_id=4242)
        cls.mgmt.refresh_from_db()

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def _get(self, view, **kwargs):
        """Drive *view* for real with an authenticated request."""
        request = self.factory.get("/")
        request.user = self.superuser
        return view.as_view()(request, **kwargs)

    def test_the_job_endpoints_answer_502_not_500(self):
        from netbox_nso_plugin.views import NSODeviceJobsView, NSOJobStatusView

        for label, view, kwargs in (
            ("NSOJobStatusView", NSOJobStatusView, {"job_id": 7}),
            ("NSODeviceJobsView", NSODeviceJobsView, {"pk": self.device.pk}),
        ):
            with self.subTest(site=label):
                resp = self._get(view, **kwargs)

                self.assertEqual(resp.status_code, 502)
                self.assertEqual(json.loads(resp.content)["error"], _PUBLIC_INVALID_RESPONSE)

    def test_the_onboarding_poll_stays_retryable_instead_of_failing_the_row(self):
        """A malformed poll answer is undecided, not a terminal verdict.

        The row must keep waiting for a well-formed one. Before the boundary check the
        scalar reached ``err.get("message")`` and raised AttributeError out of the poll, so
        the row stayed provisioning by accident and every later poll raised again.
        """
        from netbox_nso_plugin.onboarding import advance_provisioning

        mirror_update(self.mgmt, onboard_status="provisioning", onboard_job_id="job-42")
        self.mgmt.refresh_from_db()

        result = advance_provisioning(self.mgmt)

        self.assertEqual(result["status"], "provisioning")
        self.assertEqual(result["poll_error"], _PUBLIC_INVALID_RESPONSE)
        self.mgmt.refresh_from_db()
        self.assertEqual(self.mgmt.onboard_status, "provisioning")
