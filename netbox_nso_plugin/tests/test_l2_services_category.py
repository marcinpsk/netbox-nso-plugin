# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""M37 P1: read-only L2 services category — reconcile, tile count, and view render."""

from __future__ import annotations

from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

User = get_user_model()

_PAYLOAD = {
    "services": [
        {
            "service_name": "TL",
            "service_type": "epipe",
            "service_id": 4022,
            "saps": [
                {"sap_id": "lag-60:3999", "port": "lag-60", "outer_tag": 3999, "inner_tag": None},
                {"sap_id": "lag-60:4022", "port": "lag-60", "outer_tag": 4022, "inner_tag": None},
            ],
        },
        {
            "service_name": "701",
            "service_type": "vpls",
            "service_id": None,
            "saps": [{"sap_id": "1/1/c28/1:100.10", "port": "1/1/c28/1", "outer_tag": 100, "inner_tag": 10}],
        },
    ]
}


def _make_device_mgmt(tag="l2"):
    mfg = Manufacturer.objects.create(name=f"{tag}Mfg", slug=f"{tag}mfg")
    dt = DeviceType.objects.create(manufacturer=mfg, model=f"{tag}Dev", slug=f"{tag}dev")
    role = DeviceRole.objects.create(name=f"{tag}Role", slug=f"{tag}role")
    site = Site.objects.create(name=f"{tag}Site", slug=f"{tag}site")
    device = Device.objects.create(name=f"{tag}-rtr", device_type=dt, role=role, site=site)
    inst = NSOInstance.objects.create(name=f"{tag}-inst", adapter_instance_id=f"{tag}-inst")
    mgmt = NSODeviceManagement.objects.create(
        device=device, nso_instance=inst, nso_device_name=f"{tag}-rtr", adapter_device_id=77
    )
    return device, mgmt


class TestL2ServicesReconcileAndCount(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.mgmt = _make_device_mgmt("rc")

    def test_reconcile_category_returns_services(self):
        from netbox_nso_plugin.reconcile import reconcile_category

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            ctx = reconcile_category(self.device, self.mgmt, "l2_services")
        assert {s["service_name"] for s in ctx["l2_services"]} == {"TL", "701"}

    def test_tile_count_sums_saps(self):
        from netbox_nso_plugin.summary import _l2_service_count

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", return_value=_PAYLOAD):
            assert _l2_service_count(self.mgmt) == {"total": 3, "drift": 0, "pending": 0}

    def test_tile_count_zero_on_adapter_error(self):
        from netbox_nso_plugin.summary import _l2_service_count

        with patch("netbox_nso_plugin.adapter_client.get_l2_services", side_effect=Exception("down")):
            assert _l2_service_count(self.mgmt)["total"] == 0


class TestL2ServicesCategoryView(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device, cls.mgmt = _make_device_mgmt("vw")
        cls.user = User.objects.create_superuser(username="l2admin", password="pw", email="l2@test.x")  # noqa: S106

    def test_category_renders_services_and_filter(self):
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
        assert "1/1/c28/1:100.10" in html  # QinQ SAP
        assert "100.10" in html  # outer.inner encap rendered
        assert "data-l2-filter" in html  # search box present
        assert "vpls" in html
