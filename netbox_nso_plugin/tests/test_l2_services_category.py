# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Task 3: L2 services category — reconcile wiring, counts, accept, view render."""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance, NSOL2SapState

User = get_user_model()

_PAYLOAD = {
    "services": [
        {
            "service_name": "TL",
            "service_type": "epipe",
            "service_id": 4022,
            "saps": [{"sap_id": "lag-60:3999", "port": "lag-60", "outer_tag": 3999, "inner_tag": None}],
        },
        {
            "service_name": "701",
            "service_type": "vpls",
            "service_id": None,
            "saps": [{"sap_id": "1/1/c31/3:701", "port": "1/1/c31/3", "outer_tag": 701, "inner_tag": None}],
        },
    ]
}


def _make(tag="l2c", *, manage_l2=False):
    mfg = Manufacturer.objects.create(name=f"{tag}Mfg", slug=f"{tag}mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{tag}Dev", slug=f"{tag}dev")
    role = DeviceRole.objects.create(name=f"{tag}Role", slug=f"{tag}role")
    site = Site.objects.create(name=f"{tag}Site", slug=f"{tag}site")
    device = Device.objects.create(name=f"{tag}-rtr", device_type=dt, role=role, site=site)
    inst = NSOInstance.objects.create(name=f"{tag}-inst", adapter_instance_id=f"{tag}-inst")
    mgmt = NSODeviceManagement.objects.create(
        device=device,
        nso_instance=inst,
        nso_device_name=f"{tag}-rtr",
        adapter_device_id=77,
        manage_l2=manage_l2,
    )
    Interface.objects.create(device=device, name="lag-60", type="lag")
    Interface.objects.create(device=device, name="1/1/c31/3", type="other")
    return device, mgmt


class TestL2ServicesReconcileAndCount(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.mgmt = _make("rc", manage_l2=True)

    def test_reconcile_category_creates_states(self):
        from netbox_nso_plugin.reconcile import reconcile_category

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            ctx = reconcile_category(self.device, self.mgmt, "l2_services")
        rows = {r.service_name: r for r in ctx["l2_sap_states"]}
        assert set(rows) == {"TL", "701"}
        assert rows["701"].status == "imported"  # port exists → terminated, unowned → imported

    def test_category_counts_from_overlay(self):
        from netbox_nso_plugin.reconcile import reconcile_category
        from netbox_nso_plugin.summary import _category_counts

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            reconcile_category(self.device, self.mgmt, "l2_services")
        counts = _category_counts("l2_services", self.device, self.mgmt)
        assert counts["total"] == 2

    def test_tile_appears_when_manage_l2_and_rows(self):
        from netbox_nso_plugin.reconcile import reconcile_category
        from netbox_nso_plugin.summary import category_summaries

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            reconcile_category(self.device, self.mgmt, "l2_services")
        keys = {c["key"] for c in category_summaries(self.device, self.mgmt)}
        assert "l2_services" in keys

    def test_tile_hidden_without_manage_l2(self):
        from netbox_nso_plugin.summary import category_summaries

        dev, mgmt = _make("noflag", manage_l2=False)
        keys = {c["key"] for c in category_summaries(dev, mgmt)}
        assert "l2_services" not in keys


class TestL2ServicesCategoryViewAndAccept(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.mgmt = _make("vw", manage_l2=True)
        cls.user = User.objects.create_superuser(username="l2admin", password="pw", email="l2@test.x")  # noqa: S106

    def test_category_renders_states_and_accept(self):
        self.client.force_login(self.user)
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "l2_services"},
        )
        # Paged categories read last-synced state; ?refresh=1 forces a live reconcile
        # (here driven by the mocked adapter) so the rows materialise on this request.
        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            resp = self.client.get(url, {"refresh": "1"})
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "lag-60:3999" in html
        assert "nso-cat-filter" in html  # server-side pager search box present
        assert "vpls" in html

    def test_category_compacts_service_and_sap_identity(self):
        self.client.force_login(self.user)
        from netbox_nso_plugin.reconcile import reconcile_category

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            reconcile_category(self.device, self.mgmt, "l2_services")
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "l2_services"},
        )

        response = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SAP endpoint")
        self.assertContains(response, "Last Synced")
        self.assertContains(response, "lag-60:3999")
        port = Interface.objects.get(device=self.device, name="lag-60")
        self.assertContains(response, f'href="{port.get_absolute_url()}"')
        self.assertNotContains(response, "<th>Type</th>", html=True)
        self.assertNotContains(response, "<th>Port</th>", html=True)
        self.assertNotContains(response, "<th>Encap</th>", html=True)
        self.assertNotContains(response, "<th>L2VPN</th>", html=True)
        self.assertNotContains(response, "write-back is a later phase")

    def test_accept_marks_owned(self):
        self.client.force_login(self.user)
        from netbox_nso_plugin.reconcile import reconcile_category

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            reconcile_category(self.device, self.mgmt, "l2_services")
        # Force a drift status so Accept is meaningful.
        st = NSOL2SapState.objects.get(management=self.mgmt, service_name="701")
        NSOL2SapState.objects.filter(pk=st.pk).update(status="changed")
        url = reverse("plugins:netbox_nso_plugin:l2_accept_sap", kwargs={"pk": st.pk})
        resp = self.client.post(url)
        assert resp.status_code == 302
        st.refresh_from_db()
        assert st.accepted_at is not None
        assert st.status == "accepted"

    def test_reaccept_keeps_first_accepted_timestamp(self):
        self.client.force_login(self.user)
        state = NSOL2SapState.objects.create(
            management=self.mgmt,
            service_name="KEEP-TIME",
            service_type="epipe",
            sap_id="lag-60:100",
            port="lag-60",
            outer_tag=100,
            status="changed",
            accepted_at=timezone.now() - timezone.timedelta(days=3),
        )
        first_accepted_at = state.accepted_at

        url = reverse("plugins:netbox_nso_plugin:l2_accept_sap", kwargs={"pk": state.pk})
        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.accepted_at, first_accepted_at)

    def test_accept_rejects_service_types_the_writer_cannot_apply(self):
        self.client.force_login(self.user)
        state = NSOL2SapState.objects.create(
            management=self.mgmt,
            service_name="READ-ONLY-CPIPE",
            service_type="cpipe",
            sap_id="lag-60:100",
            port="lag-60",
            outer_tag=100,
            status="changed",
        )

        url = reverse("plugins:netbox_nso_plugin:l2_accept_sap", kwargs={"pk": state.pk})
        response = self.client.post(url)

        self.assertEqual(response.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.status, "changed")
        self.assertIsNone(state.accepted_at)

    def test_unsupported_service_type_renders_read_only_without_accept(self):
        self.client.force_login(self.user)
        state = NSOL2SapState.objects.create(
            management=self.mgmt,
            service_name="READ-ONLY-IPIPE",
            service_type="ipipe",
            sap_id="lag-60:200",
            port="lag-60",
            outer_tag=200,
            status="changed",
        )
        url = reverse(
            "plugins:netbox_nso_plugin:device_nso_category",
            kwargs={"pk": self.device.pk, "key": "l2_services"},
        )

        response = self.client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "read-only")
        accept_url = reverse("plugins:netbox_nso_plugin:l2_accept_sap", kwargs={"pk": state.pk})
        self.assertNotContains(response, accept_url)
