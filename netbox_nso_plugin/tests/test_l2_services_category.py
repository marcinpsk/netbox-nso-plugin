# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M37 P2a Task 3: L2 services category — reconcile wiring, counts, accept, view render."""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

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
        assert rows["701"].status == "in_sync"  # port exists → terminated

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
        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            resp = self.client.get(url)
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "lag-60:3999" in html
        assert "data-text-filter" in html  # shared state-pill filter present
        assert "vpls" in html

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
