# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Tests for the logging/syslog read-path: _reconcile_logging_config + category counts."""

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.test import TestCase


def _make_device(suffix="log"):
    mfg, _ = Manufacturer.objects.get_or_create(name=f"LogMfg{suffix}", slug=f"logmfg{suffix}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfg, model=f"LogDev{suffix}", slug=f"logdev{suffix}")
    role, _ = DeviceRole.objects.get_or_create(name=f"LogRole{suffix}", slug=f"logrole{suffix}")
    site, _ = Site.objects.get_or_create(name=f"LogSite{suffix}", slug=f"logsite{suffix}")
    return Device.objects.create(name=f"log-rtr-{suffix}", device_type=dt, role=role, site=site)


class TestReconcileLoggingConfig(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = _make_device("main")

    def _mgmt(self):
        from netbox_nso_plugin.models import NSODeviceManagement, NSOInstance

        inst, _ = NSOInstance.objects.get_or_create(name="log-inst", defaults={"adapter_instance_id": "log-inst"})
        return NSODeviceManagement.objects.get_or_create(
            device=self.device,
            defaults={"nso_instance": inst, "nso_device_name": "log-dev", "adapter_device_id": self.device.pk},
        )[0]

    def _payload(self, *hosts):
        return {"hosts": list(hosts), "last_refreshed_at": None, "refresh_source": "test"}

    def test_no_mgmt_returns_empty(self):
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        res = _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1"}))
        self.assertEqual(res["hosts"], [])

    def test_creates_host(self):
        self._mgmt()
        from netbox_nso_plugin.models import NSOLoggingHostState
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        res = _reconcile_logging_config(
            self.device,
            self._payload({"address": "84.116.251.86", "severity": "warning", "facility": "any", "source": "1.1.1.1"}),
        )
        self.assertEqual(len(res["hosts"]), 1)
        h = NSOLoggingHostState.objects.get(management__device=self.device, address="84.116.251.86")
        self.assertEqual(h.severity, "warning")
        self.assertEqual(h.source, "1.1.1.1")
        self.assertEqual(h.status, "imported")

    def test_full_replace_deletes_absent(self):
        self._mgmt()
        from netbox_nso_plugin.models import NSOLoggingHostState
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1"}, {"address": "10.0.0.2"}))
        self.assertEqual(NSOLoggingHostState.objects.filter(management__device=self.device).count(), 2)
        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.2"}))
        addrs = set(
            NSOLoggingHostState.objects.filter(management__device=self.device).values_list("address", flat=True)
        )
        self.assertEqual(addrs, {"10.0.0.2"})

    def test_idempotent_update(self):
        self._mgmt()
        from netbox_nso_plugin.models import NSOLoggingHostState
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1", "severity": "info"}))
        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1", "severity": "error"}))
        rows = NSOLoggingHostState.objects.filter(management__device=self.device)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().severity, "error")

    def test_category_appears_with_counts(self):
        mgmt = self._mgmt()
        mgmt.manage_logging = True
        mgmt.save()
        from netbox_nso_plugin.summary import category_summaries
        from netbox_nso_plugin.template_content import _reconcile_logging_config

        _reconcile_logging_config(self.device, self._payload({"address": "10.0.0.1"}))
        cats = {c["key"]: c for c in category_summaries(self.device, mgmt)}
        self.assertIn("logging", cats)
        self.assertEqual(cats["logging"]["counts"]["total"], 1)
