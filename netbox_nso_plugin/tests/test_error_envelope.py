# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A caught exception's text never reaches an HTTP response.

Every adapter failure is rendered by some view, so ``adapter_client`` converts the
transport exception into an ``AdapterError`` that carries the exception TYPE only, and the
blanket ``except`` guards in ``onboarding`` follow the same rule. The full text goes to the
server log. These tests drive the real views through the real ``adapter_client`` (only the
network ``requests.Session`` is replaced) and assert the leaked text is gone from the body.
"""

from unittest.mock import patch

import requests
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Platform, Site
from django.test import TestCase, override_settings
from django.urls import reverse
from ipam.models import IPAddress

from netbox_nso_plugin.adapter_client import AdapterError
from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOPlatformNedMapping

from .test_django_views import ViewTestBase

# Shaped like something that must never be echoed to a client: a requests transport error
# repeats the request it failed on, and an unexpected internal error can repeat a row value.
_LEAK = "Bearer nso-adapter-token-abc123 must never leave the server"
_ADAPTER_LOG = "netbox_nso_plugin.adapter_client"
_ONBOARDING_LOG = "netbox_nso_plugin.onboarding"
_VIEWS_LOG = "netbox_nso_plugin.views"
_AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
_PLUGINS_CONFIG = {
    "netbox_nso_plugin": {"adapter_url": "http://adapter.invalid", "adapter_token": "envelope-test-token"}
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
        session_patch = patch("netbox_nso_plugin.adapter_client.requests.Session", _LeakingSession)
        session_patch.start()
        self.addCleanup(session_patch.stop)

    def assertEnvelopeOnly(self, body, *, expected_type="ConnectionError"):
        """The body names the exception type but carries none of its text."""
        self.assertNotIn(_LEAK, body)
        self.assertIn(expected_type, body)


class TestAdapterTransportEnvelope(_UnreachableAdapterMixin, TestCase):
    """adapter_client is where the transport exception is converted, so it is tested there."""

    def test_transport_error_text_never_enters_the_adapter_error(self):
        """The AdapterError names the transport exception type; only the log gets its text."""
        from netbox_nso_plugin.adapter_client import _request

        with self.assertLogs(_ADAPTER_LOG, level="WARNING") as logs:
            with self.assertRaises(AdapterError) as ctx:
                _request("GET", "/test")

        self.assertEqual(ctx.exception.code, "nso_unreachable")
        self.assertEnvelopeOnly(str(ctx.exception))
        self.assertTrue(any(_LEAK in line for line in logs.output), "the transport text must reach the server log")


class TestAdapterErrorEnvelopeInResponses(_UnreachableAdapterMixin, ViewTestBase):
    """Every view that renders an adapter failure serves the envelope, never the transport text."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A queryset write, so the post_save adapter push does not fire during fixture setup.
        NSODeviceManagement.objects.filter(pk=cls.mgmt.pk).update(adapter_device_id=4242)
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

    def test_no_response_carries_the_transport_exception_text(self):
        """Each site reports the failure by exception type, and logs the text server-side."""
        for label, url, method, data in self._response_cases():
            with self.subTest(site=label):
                with self.assertLogs(_ADAPTER_LOG, level="WARNING") as logs:
                    if method == "get":
                        resp = self.client.get(url, **_AJAX)
                    else:
                        resp = self.client.post(url, data, **_AJAX)
                self.assertEnvelopeOnly(resp.content.decode())
                self.assertTrue(any(_LEAK in line for line in logs.output))

    def test_onboard_status_poll_reports_the_type_not_the_transport_text(self):
        """A transient adapter outage while polling keeps the row provisioning, with no leak."""
        NSODeviceManagement.objects.filter(pk=self.mgmt.pk).update(
            onboard_status="provisioning", onboard_job_id="job-42"
        )

        with self.assertLogs(_ADAPTER_LOG, level="WARNING"):
            resp = self.client.post(self._url("onboard_status", pk=self.mgmt.pk), **_AJAX)

        body = resp.json()
        self.assertEqual(body["status"], "provisioning")
        self.assertEnvelopeOnly(body["poll_error"])


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
            patch.object(NSODeviceManagement.objects, "create", side_effect=RuntimeError(_LEAK)),
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
            patch.object(NSODeviceManagement.objects, "create", side_effect=RuntimeError(_LEAK)),
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
